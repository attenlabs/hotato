# Changelog

All notable changes to Hotato are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Every entry reports millisecond measurement error and a confusion matrix. See `docs/BENCHMARK.md`.

## [Unreleased]

## [1.13.1] - 2026-07-21

### Fixed
- **`run --stereo` no longer fabricates a FAIL on byte-identical channels.** A
  recording whose two channels carry the same signal (a channel compared to
  itself) now routes through the same **NOT SCORABLE** refusal (exit 2) that
  `trust` and `investigate` use, before any timing number is emitted. Eligible
  recordings stay byte-identical.
- **The transcript scorer refuses adversarial timestamps up front.** A negative
  timestamp (which indexed before frame 0 and crashed the scorer) and a mistyped
  huge `end` (e.g. `50000`s, which built an unbounded per-hop timeline) now each
  return a clean usage error (exit 2) instead of a traceback or a runaway
  allocation.
- **`investigate label --expect yield` on a talk-over catch gates red, as the
  docs promise.** A caught talk-over above the 1.0s prompt-yield ceiling
  auto-pins `max_talk_over_sec`, so the contract fails the build (exit 1) on the
  captured magnitude; prompt-scale yields stay green, and an explicit
  `--max-talk-over` still wins.
- **`init --auto` prints a verify command that works on its own scaffold.** The
  suggested next command is now the same shell-guarded no-op the generated CI job
  runs, so it succeeds on a freshly scaffolded, empty contracts directory.

### Changed
- **The bundled missed-interruption demo is an audible barge-in.** The `fd-01`
  demo clip is a deterministically-synthesized two-channel barge-in in which the
  caller takes the floor at 2.0s and the agent talks over them for a measured
  2.65s. It is labeled as a synthesized fixture, and the demo report, SDK
  fixtures, and docs report the same measured numbers.
- **Voice-first README and docs; refreshed PyPI page.** The README is streamlined
  to the voice-first identity (pain-led hero, a four-plane Catch / Test / Gate /
  Observe table, a tight quickstart and CI block), and the PyPI long-description
  and `llms` surfaces are regenerated to match.

## [1.13.0] - 2026-07-21

### Added
- **`hotato observe`: local-first LLM observability.** A command group that
  offers the value of hosted observability tools with nothing leaving your
  machine. `observe capture -- <command>` runs any OpenTelemetry-emitting
  process with export wired to a local file sink, then ingests and summarizes
  its spans offline. `observe cost` rolls up token usage per model from an
  ingested trace and estimates USD from a local price table (tokens are facts;
  a field no span reported is "not captured", never 0; USD is a labeled estimate
  from your table). `observe percentiles DIR` and `observe report DIR` compute
  p50/p90/p99 latency and render a self-contained dashboard across a corpus of
  traces; uncaptured hops are excluded with a shown count, never zeroed.
- **`hotato contract verify --pr-comment`: a visual PR comment.** Renders the
  gate verdict, the caught moment, the measured number, and a deterministic
  caller/agent barge-in timeline as Markdown for a pull-request comment. The
  Action posts or updates one comment when a token is present; fail-open, so a
  comment problem never changes the verify exit code.

### Added
- **`hotato gauntlet`: a bundled turn-taking stress suite + a shareable badge.**
  Ten standardized, seeded, byte-reproducible stress cases (hard barge-in,
  backchannel-not-a-floor-take, filler-start interruption, urgent correction,
  telephony 8kHz, sustained talk-over, agent-audio-bleed, rapid turn-taking, and
  two seeded robustness variants) scored over the packaged reference recordings.
  `hotato gauntlet badge` renders a self-contained "Gauntlet N/10" SVG derived
  from a real run. Scores the bundled deterministic stimulus.
- **`hotato init --auto`: zero-config onboarding.** Detects the voice-agent
  framework from the project's dependencies, locates candidate recordings, and
  scaffolds the config and CI gate pre-filled for the detected framework. Read
  only; refuses cleanly with the manual path when nothing is detected.

### Fixed
- **The failure card heroes the measured number, never `?s`.** A yield contract
  the agent failed by never yielding has `seconds_to_yield` null by definition;
  the share card now leads with the measured talk-over and states plainly that
  the agent never yielded, instead of rendering a meaningless `?s` on the most
  shared asset.
- **`hotato investigate` headlines the most story-worthy catch.** Candidate
  salience is not comparable across kinds (seconds of acoustic overlap vs
  seconds of dead air), so a passive end-of-call trailing silence could
  out-rank a real missed barge-in and headline a non-story. The headline now
  leads by the canonical kind severity, so the agent talking over or stepping
  on the caller leads over a trailing gap. The full candidate list and every
  reference keep their salience order.

## [1.12.1] - 2026-07-21

### Changed
- **Positioning copy across README, PyPI, and llms.txt** now leads with
  deterministic regression testing for voice and chat agents, organizes the
  capability surface into the Catch, Exercise, Decide, and Gate workflows, adds
  the proof strip (byte-reproducible verdicts, content-addressed contracts,
  git-bisect predicates, agent-native over local MCP), and states contract
  verification in Captured-Evidence terms (a contract preserves the captured
  failure and re-measures it under the pinned policy). No code changes.

## [1.12.0] - 2026-07-21

### Added
- **Compliance assertion packs + the `order` kind.** Curated, deterministic
  assertion packs (`hotato assert packs`, `hotato assert run --pack NAME`):
  required-disclosure, prohibited-language, pii-leak, and
  identity-verification-order, built only from deterministic kinds. Adds a new
  `order` kind (the first transcript turn matching `before` must precede the
  first matching `after`). These are configurable transcript checks, not a
  certification.
- **Scenario variables and branches.** `scenario.v1` gains `variables:`
  (templated `{name}` substitution into caller lines) and `branches:` (a node
  graph whose every root-to-leaf path is deterministically enumerated), both
  expanding into the existing `simulate --matrix` runner with stable derived
  seeds. Cycles, unknown nodes, and unbound variables are refused.
- **Cross-run failure clustering (`hotato diagnose --fleet DIR`).** Scans a
  folder of run and investigate results, fingerprints each failure
  deterministically (dimension, direction, magnitude bucket, config hash when
  present), and groups them into clusters ranked by count with member
  references. Text and JSON; a malformed envelope is refused, never a crash.
- **Composite and conditional assertions (`formula`, `when:`).** A `formula`
  assertion combines other named assertions' results with `and`/`or`/`not`,
  parentheses, and a weighted-sum threshold form, parsed without `eval()`. Any
  assertion may carry a `when:` precondition that skips it (INCONCLUSIVE) unless
  the referenced assertions passed. Unknown names, self-references, and cycles
  are refused up front; a referenced INCONCLUSIVE propagates, never a guess.
- **`hotato baseline check`.** Gates drift between a committed baseline result
  and a candidate result under per-dimension tolerances (`response_gap_sec:
  "+10%"`, `seconds_to_yield: "+0.05"`), per event and per dimension, exit 0/1/2
  with JUnit output. A one-sided or absent dimension is refused, never scored 0.
- **`hotato simulate --chat URL`.** Drives the scripted deterministic caller
  against a chat agent over local HTTP and records a timestamped transcript in
  the shape `hotato investigate --transcript` consumes, so a text agent goes
  from scenario to CI gate with no audio. Localhost by default; a non-local URL
  requires `--egress-opt-in`.
- **Acoustic health metrics (`hotato investigate`, audio path).** Per-channel
  SNR, percent-silence, energy-burst rate, and clipping fraction, reported as an
  `acoustic` block. Each states it is a signal measure, not intelligibility or
  intent; the transcript path omits it.
- **Robustness battery (`hotato battery robustness`).** Renders a recording at
  staged SNR, clipping, dropout, and jitter, scores each stage, and reports how
  the timing signals move across degradation. Byte-reproducible for a fixed seed.
- **Fleet latency percentiles.** p50/p90/p99 for `response_gap_sec`,
  `seconds_to_yield`, and `talk_over_sec` across a corpus, in the team trend and
  report. Nulls are excluded with a shown count, never treated as 0.
- **PR result reporting (`hotato contract verify --step-summary FILE`).**
  Appends a tight Markdown summary of the verify (verdict line, pass/fail
  counts, top failing contracts with measured timing) to FILE, made for
  GitHub's `$GITHUB_STEP_SUMMARY`. The root Action's contracts run uses it to
  put the verdict on the PR's job summary, ahead of the five-lane summary.
  Presentation only and fail-open: a render or write problem reports on
  stderr and never changes the verify exit code, and the Action's exit-code
  contract is untouched (an exact older hotato pin without the flag runs
  with its unchanged argv).
- **Score text and chat agents from a transcript (`hotato investigate --transcript FILE.json`).**
  Accepts a timestamped, speaker-labeled transcript of `{role|speaker, text,
  start, end}` turns and scores them through the existing turn-taking scorer with
  no recording, so agents without two-channel audio are scorable in the same
  complaint-to-CI-gate loop. The scoring engine is unchanged (the transcript is
  quantized onto the score hop grid and run through the diarize stub-backend
  seam). Because a sequential transcript cannot represent acoustic overlap,
  `talk_over_sec` and `premature_start_sec` are reported as null with a reason
  and the overlap-based candidate kinds are suppressed; `did_yield`,
  `seconds_to_yield`, `response_gap_sec`, and `caller_onset_sec` are reported,
  derived from the turn timestamps.

## [1.11.0] - 2026-07-20

### Added
- **Curated seeded persona pack (`hotato-voice-personas`).** Ships seven common
  voice-agent test cases inside the wheel (missed barge-in, backchannel that is
  not a floor-take, dead-air/silence, over-eager early response, caller
  talk-over, and fast/slow pacing), so a bare `pip install hotato` can list and
  run them by name with no file authoring: `hotato simulate --list` and
  `hotato simulate barge-in-missed --out ./sim`. Each entry is a real
  `hotato.scenario.v1` the existing deterministic caller renders; the pack adds
  a loader and manifest index, no engine, and scores nothing on its own.
- **`hotato investigate --demo`.** A no-recording first-run on-ramp: runs the
  scorer on a bundled two-channel demo call so a new user reaches the caught
  moment and the `investigate label` step with nothing of their own. It is the
  same scorer on a packaged sample (resolved as package data, so it works from
  any directory), mutually exclusive with a positional recording / `--stack` /
  `--call-id`.
- **`hotato contract verify --notify URL`.** Opt-in, repeatable webhook that
  POSTs a share-safe run-summary when the gate finishes: the pass/fail counts
  (`passed`/`failed`/`tampered`/`refused`/`assertions_failed`, kept as separate
  fields) and the top failing contracts' ids + measured timing, plus a Slack
  `text` line. No audio, credentials, transcript, or file paths leave the
  machine. Off by default; a non-http(s) scheme is refused before the re-score;
  delivery is fail-open, so a down webhook never changes the verify exit code.
  Reuses the existing `notify.py` path; see `docs/EGRESS.md`.

### Changed
- **`hotato start --demo --format json` leads with the activation on-ramp.** The
  JSON `next_commands` now lead with the `hotato investigate <event.wav>` step
  the human closing already prints (score the scorable call the demo just
  wrote) before the CI-scaffold commands, so `--format json` (agent) consumers
  get the same first step a person sees.

### Fixed
- **`hotato simulate <name> --out DIR` is byte-identical every run.** The
  single-run path stamped the manifest `created_at` from the wall clock, so the
  documented command was not byte-reproducible (only `transcript.json` /
  `trace.jsonl` were). It now defaults `created_at` to a reproducible instant
  (SOURCE_DATE_EPOCH-style), matching the `--matrix` path, so the full bundle
  (`conversation.json` included) matches across runs; `--created-at` or
  `$SOURCE_DATE_EPOCH` still pins a real timestamp.

## [1.10.1] - 2026-07-20

### Changed
- **The first catch reads as one shareable moment.** `hotato start --demo`
  closes with a next step that scores the demo's own `event.wav`, so the
  first command a new user runs already has a call to score. The catch leads
  with a plain-English sentence and two labeled signals, talk-over and
  response gap; `hotato investigate` leads with the caught moment; and the
  failure-record card renders the caller/agent overlap timeline.

## [1.10.0] - 2026-07-20

### Added
- **Reactive barge-in caller plan (`reactive_barge_in_plan`).** Builds a
  caller program that listens on an agent-speech-onset event, waits, says its
  line, then hangs up, timing the interrupt from the agent's speech onset
  rather than a fixed clock. Uses the existing `lifecycle` event kind, so the
  caller-plan schema and the CLI are unchanged. Hermetic-only; wiring to a
  live drive call against the deployed agent stays operator-gated.
- **Config-hash-bound release proof (`meets_release_proof`).** Adds
  `REQUIRED_FOR_RELEASE_PROOF` (the paired-proof dimensions plus
  `deployment_identity`) alongside `meets_paired_proof`: a release proof is
  stronger than a paired proof, passing only when the candidate deployment
  identity is config-hash-bound. Optional agent / deployment / config-hash
  identity threads through `fix_trial` into the pinned manifest; the defaults
  preserve byte-identical behavior.
- **`pr create` accepts contract bundles.** `--fixtures` now takes a
  `<id>.hotato` contract bundle from `hotato investigate label` /
  `hotato contract create` (or a directory of them) as well as a fixtures
  directory, detected by shape, never a flag. The PR body is built from each
  bundle's own `contract.json` (id, expected behavior, measured outcome,
  replay command) and the bundle is staged WHOLE under
  `tests/hotato/contracts/`, byte-identical (the bundle is
  content-addressed; nothing inside it is rewritten), with
  `hotato contract verify tests/hotato/contracts/` as the CI gate. The
  refusal on a directory that is neither shape names both accepted shapes.

### Changed
- **`investigate label` prints one next step that completes.** The follow-up
  guidance after a CI-ready contract is the exact `hotato pr create` command
  for the bundle it just wrote, and `docs/BAD-CALL-TO-CI.md`'s provider-call
  path now runs end-to-end as written (`investigate` -> `investigate label`
  -> `pr create`), with the `fixture promote` sequence kept as the
  alternate.

### Changed
- **`start --demo` ends with one clear next step.** The guided first run's
  closing collapsed from a fan of promote/run/card/say-do commands to a single
  next action, scoring your own call with `hotato investigate`, plus one
  condensed line for wiring it into CI. The full command set stays in the
  `--format json` `next_commands` payload, so agents get everything while a
  new developer sees exactly one step.

### Fixed
- **The read-only SQL guard cannot be hung.** `SqlStateAdapter`'s
  read-only check matched leading whitespace with a catastrophic-backtracking
  regex (over 5s on ~1KB of leading whitespace); it is now a linear scan that
  accepts exactly the same queries (verified across 27 equivalence cases) and
  stays O(n) into the millions of characters. Also swapped two insecure
  `tempfile.mktemp` calls in the test suite for `mkstemp`.
- **Auto-opened reports now open on Ubuntu.** `hotato doctor` and `hotato
  demo` wrote their HTML report into the system temp dir and opened
  `file:///tmp/...`; the default browser on Ubuntu is a snap that cannot read
  `/tmp`, so the report opened to "file not found" (and `webbrowser.open`
  reported success, so no fallback printed the path). The reports now default
  into the working directory, and the open helper stages any
  browser-unreachable path under a non-hidden `$HOME` directory before
  opening.
- **`docs/RECAPTURE.md` signing note corrected.** The doc said bundle
  signing was unimplemented; `attest.py` (HMAC-SHA256) and `sign.py`
  (Ed25519) provide bundle authenticity signing today, so the note now
  describes the available signature (an unsigned bundle stays
  unauthenticated).

## [1.9.0] - 2026-07-17

### Added
- **Telephony lifecycle plane: `hotato telephony`.** `capabilities`,
  `create`, `status`, `cancel`, and `export` drive one provider-agnostic
  call-lifecycle controller with per-provider declared capabilities,
  bounded provider pulls shared with `capture`/`drive`, and a redacted
  lifecycle receipt (`telephony-receipt.v1`) whose export states lifecycle
  facts only, never a media-delivery claim. Each subcommand carries the
  documented exit-code contract in `hotato describe`. Docs:
  `docs/TRANSPORT-RUNTIME.md`.
- **Caller plane: `hotato caller run/verify`.** Runs a scripted, hybrid, or
  generative caller program against a target transport and packages every
  artifact into a content-addressed caller package (`caller-plan.v1`,
  `caller-result.v1`, `caller-session.v1`); `verify` independently
  reproduces the package and hash-compares every bound artifact. Speech
  comes from a pinned local Piper TTS engine (`docs/PIPER-CALLER-TTS.md`);
  LiveKit session support stays an optional, lazily loaded integration
  (`docs/LIVEKIT-CALLER-SESSION.md`, `docs/GENERATIVE-CALLER.md`,
  `docs/CALLER-SIDECAR-PROTOCOL.md`).
- **Load plane: `hotato load telephony|caller run/verify`.**
  Closed-concurrency staircase and open-arrival workloads with a per-child
  evidence package (`load-plan.v2`, `load-result.v2`, `load-evidence.v1`),
  queue delay, dropped starts, blocked/error rate, completion, and
  endpoint-match rate reported separately (never blended), and an
  adversarial offline verifier that refuses tampered, replayed, partial, or
  wrong-child packages. Docs: `docs/CALLER-LOAD.md`,
  `docs/LOAD-AND-RECOVERY.md`.
- **Production evidence plane: `hotato production`.** A loopback evidence
  gateway (`serve`, `ingest`, `status`, `finalize`, `maintain`, `alerts`,
  `audit`, `delete`) over a local SQLite store with a verifiable audit
  chain, plus `export-regression`/`verify-regression` for
  offline-verifiable regression candidates promoted through the atomic
  no-replace publish. `hotato serve --production-db` projects manifests and
  alerts from that store read-only into `/health`, without payload access
  and without importing production rows into the fleet registry. Docs:
  `docs/PRODUCTION-MONITORING.md`; deployment reference (Compose topology,
  loopback binds, firewall manifest): `deploy/control-plane/`.
- All four planes above are offline-verified: the shipped suite exercises
  them hermetically against recorded provider shapes and local processes.
  Live carrier, provider, sustained-load, and production-durability
  behavior is qualified only by the external acceptance gates
  (`deploy/control-plane/README.md`).
- **`hotato start --demo` act two: the say-do check.** After the timing act
  (unchanged, still first), the guided first run now evaluates ONE say-do
  conversation check end to end, offline, over a bundled scripted
  conversation (`data/demo/saydo/`, mirroring the reference agent's
  `refund-claimed-not-issued` job): the agent says the refund was sent; the
  trace carries no `issue_refund` tool span and the post-call state's
  `refund_status` stays `"none"`, so the check FAILS by design through the
  same `evaluate_conversation_test` path `hotato test run` drives. Every
  packaged file is verified against the sha256 its manifest records, the
  bundle plus the evaluated `hotato.test-run.v1` result land under
  `saydo/` in `--dir`, the printed replay command
  (`hotato test run saydo/test.json ...`) exits 1 like the CI gate it is,
  and the narrated catch is checked against the evaluated results before it
  is printed (claim assertion PASS, tool + state assertions FAIL) -- a
  mismatch fails the run loudly instead of shipping an unbacked story.
  `--format json` gains a `saydo` block. `start --demo` still exits 0.
  Docs: `docs/START.md`.
- **`hotato card`: the say-do failure card (sixth kind).** A `hotato test
  run` result (kind `hotato.test-run`) whose deterministic lane failed a
  tool/state evidence assertion (`tool_result`, `tool_call`, `tool_error`,
  `http_result`, `state`, `state_change`) now auto-detects into a
  claim-vs-evidence card: the failing assertion's id and kind, its span
  refs when the evaluator recorded any, and its share-safe `public_reason`
  (allowlisted structured fields only -- never transcript text, a tool
  payload, or a state value). The failing outcome-tagged evidence assertion
  leads deterministically; a result with no failing tool/state evidence
  assertion is refused (exit 2). Same SVG invariants as the other five
  kinds: byte-deterministic, no timestamp, no accuracy number, inline color
  only, redacted by default. A committed example
  (`docs/assets/cards/say-do-card.svg`) joins the generator and its
  lockstep test. Docs: `docs/CARDS.md`.
- **`http_result`: a deterministic HTTP-exchange assertion kind.** Reads
  `http_exchange` spans already present in the ingested
  `hotato.voice_trace.v1` trace (`method`, `url`, `status_code`, `response`)
  and checks the declared method, URL regex, status set, and response subset.
  Evaluation never performs a request; the kind shares the Authority-1 wall
  with `tool_result`/`tool_error` (no transcript read, no model path),
  reports INCONCLUSIVE when no trace was supplied, and projects into Failure
  Records as `http_exchange` outcome evidence with redacted request/response
  classes. The counterexample compiler refuses an `http_result` target (the
  scripted simulator emits no exchange evidence). Docs: `docs/ASSERTIONS.md`.
- **`hotato.errors.rename_no_replace`: atomic no-replace publish.** Publishes
  a temp file or directory at its destination in one atomic step and refuses
  an existing destination with `FileExistsError`. Capability-gated, never
  platform-named: a libc no-replace rename syscall where the runtime exposes
  one, else an `os.link` + `os.unlink` file publish or a mkdir-claim
  directory publish (Windows included). `hotato regression prepare` promotes
  its bundle through it, so a destination that appears between the up-front
  existence check and the rename is refused, never clobbered.
- **`hotato run --snr-gate-db`: opt-in low-SNR scorability gate.** Below a
  measurable per-channel SNR the energy VAD's dynamic-margin cap (threshold =
  peak minus `dyn_margin_db`) saturates, agent activity never ends, and a
  correct yield scores as a false 3.0 s talk-over. Measured on the reference
  fixture: uniform noise flips the verdict between 19 and 18 dB per-channel
  SNR, babble between 21 and 20 dB; the hardest shipped pass tier (gold noise
  family) bottoms out at a 23.8 dB noise floor. The gate estimates each
  channel's stationary SNR deterministically and refuses to score
  (not-scorable, reason `low-snr`, the standard exit-2 contract) when either
  estimate falls below the floor; bare `--snr-gate-db` uses 22.0, which
  equals `dyn_margin_db`, the geometric constant of the cliff. Off by
  default, and the default output is byte-identical with the gate off.
  Curve and mechanism: `docs/BENCHMARK.md` ("Noise floor and the verdict
  cliff").
- **`hotato bench run` / `hotato bench verify`.** A versioned freeze of the
  shipped scenario batteries (the packaged battery plus the four
  `corpus/suites/` tiers), pinned by content hash. `bench run` scores one
  battery end to end and writes a content-addressed result (per-suite pass
  counts, per-signal ms-error distributions, confusion cells; no blended
  score anywhere, code-enforced). `bench verify` re-executes the pinned
  battery and hash-compares the canonical result bodies: exit 0 on a match,
  1 when re-execution does not reproduce the stored result, 2 (refused) on
  a malformed or tampered result, an unknown battery, or a local battery
  whose content hash differs from the pinned one. Protocol:
  `docs/BENCH-SPEC.md`.
- **`spec/`: the contract wire format as an open spec.** The shipped
  `contract.json` JSON Schema (`spec/contract.schema.json`, a byte-identical
  copy of `src/hotato/schema/contract.v1.json`, kept in lockstep by test),
  the stability promise (`spec/README.md`), and the exact canonical-bytes +
  content-addressing rules as implemented, with file and function citations
  (`spec/CANONICALIZATION.md`).

### Fixed
- **Windows portability of the artifact store.** The store's openat-style
  containment primitive (trusted directory descriptors + `dir_fd`-relative
  operations) has no Windows equivalent; platforms without `dir_fd` support
  now take path-based branches whose containment rests on a realpath prefix
  check, failing closed on escapes. Raw `os.open` byte I/O gains `O_BINARY`
  (zero on POSIX) so the Windows CRT's text translation cannot rewrite bytes
  under content addressing. POSIX behavior is unchanged.

## [1.8.1] - 2026-07-16

### Fixed
- **State-verdict evidence binding.** A `state` outcome verdict now binds to
  the specific evidence IDs it was judged on, and `verify` REFUSES (exit 2)
  when a bound evidence ID is missing from the record or does not cover the
  asserted state -- a verdict can no longer ride on evidence that was never
  about it.
- **File-based runs record `origin: fixture`.** Runs scored from local files
  are recorded as `fixture`, never `real`; the serve views accept and label
  the `fixture` origin and keep the three origins (`real` / `simulated` /
  `fixture`) unmerged everywhere, so provenance survives from ingest to
  dashboard.
- **Release taglock toolchain detection.** The build-exercising release tests
  now require a real importable `build` module (`spec.origin is not None`)
  instead of being fooled by a stray `build/` namespace directory -- fixes the
  `sdist-guard` CI job, which failed on `python -m build` inside the extracted
  sdist tree.

### Changed
- **README lead: "Regression testing for voice agents."** The category line
  now leads every surface (README, PyPI, GitHub About); the wedge line sits
  second. How-it-works flowchart is top-down so GitHub's mermaid controls
  no longer overlap the verdict boxes; banner sized to 442 px.

## [1.8.0] - 2026-07-15

### Added
- **The Failure Record share loop.** A failed, inconclusive, or errored test
  now yields one share-safe `hotato.failure-record.v1` artifact answering what
  failed, the evidence, the owning CI lane, and how to verify it -- rendered as
  JSON, Markdown, HTML, and SVG.
  - Evidence-specific, share-safe headlines: a controlled `public_reason`
    projects the lane and one bounded observed fact (e.g. `Conversation
    failed: Agent did not yield; measured talk-over was 0.25 s.`) from
    allowlisted structured fields only -- the raw evaluator reason never enters
    a share-safe record. Every generated record validates against the oracle
    and the shipped JSON Schema.
  - `hotato record verify RECORD [--evidence-root DIR]`: the public-reader
    command. Checks schema, content address, authority wall, reproduction
    contract, reliability semantics, and share-safe privacy; opens no socket
    and mutates no file.
  - `hotato record render SOURCE --all [--limit N]`: renders one record per
    non-passing unit into digest-named directories under a closed,
    deterministic `hotato.failure-record-index.v1`.
  - `hotato start --demo` emits the canonical Failure Record automatically and
    scaffolds the durable starter next-step.
  - The GitHub Action renders one share-safe Failure Record per non-passing
    unit by default (`render-records`, `record-limit`), with digest-scoped
    contained output and record counts in its summary. It performs no upload,
    comment, notification, telemetry, network call, or permission escalation,
    and a renderer error can never change the evaluation's exit code.
  - A restrained one-line footer (`MIT · hotato.dev · Try it: uvx hotato
    start --demo`) plus a version-pinned verify command appears in Markdown,
    HTML, and SVG; the JSON stays a pure evidence object.
- **Public Voice Failure Atlas reproduction.** Each published Atlas record
  carries structured `reproduction_metadata` (exact hotato version, committed
  bundle path and digest, selector, working directory, expected record id).
  The displayed command chain is generated from that metadata, and a
  clean-directory run of it -- `contract create` -> `contract verify` ->
  `record render`, with no stored result injected and no package version
  patched -- reproduces the record's own published `record_id`. Version, asset,
  command, or terminal-id drift fails publication; superseded content addresses
  are retained as history.

### Changed
- **Release-integrity and fleet hardening (27 fixes).** Source archives build
  only from the immutable tagged tree and clear stale `dist/` first; a test
  rejects any non-git-tracked sdist member. Human-authority labels are gated
  behind an explicit reviewer and attestation, and recompute is pinned to the
  manifest's scorer config against the label's own audio. Verdicts gate on
  cross-channel leakage, `--confirm-channels` clears only a channel swap (never
  crosstalk), and MCP tools return the uniform control envelope on bad input.
  The fleet store content-addresses via a single-stream publish with
  no-follow containment, receipt-gated clone deletion, and atomic mint; Action
  outputs write with no-follow exclusive semantics; the Retell webhook template
  verifies the documented `v=,d=` signature.

## [1.7.0] - 2026-07-14

### Added
- **`hotato.sdk`: the typed Python SDK.** Frozen dataclasses over the same
  code paths the CLI uses, fields verbatim from the CLI JSON: `run_suite`,
  `run_single`, `verify_contracts`, `investigate`, `compile_counterexample`,
  `verify_counterexample`, `transcribe`. The package ships `py.typed`, so a
  type checker reads it as typed. See `docs/SDK.md`.
- **TypeScript SDK at `sdk/typescript`.** A zero-dependency typed client that
  drives the CLI's JSON contract from Node: `runSuite`, `verifyContracts`,
  `compileCounterexample`, `predicate` with bisect exit semantics, and typed
  refusal errors. Tested against the CLI on the bundled fixtures.

- **Release integrity.** Source archives build only from the tagged tree via
  `scripts/build_release.py` (a `git archive` export at a fixed umask), and a
  test rejects any sdist member that is not git-tracked. Fleet contracts seal
  the real reviewer, rationale, stack, and candidate into the signed record,
  derive collision-free ids, and label atomically. `demo`/`doctor --format
  json` write exactly one JSON document to stdout. OS classifiers name Linux
  and macOS, the platforms the suite runs on.

## [1.6.2] - 2026-07-14

### Added
- **`hotato init ci`: the turn-taking gate for GitLab CI, Jenkins, Azure
  Pipelines, and CircleCI.** `hotato init ci --system
  {gitlab,jenkins,azure,circleci}` writes the one canonical config each
  system reads (`.gitlab-ci.yml`, `Jenkinsfile`, `azure-pipelines.yml`,
  `.circleci/config.yml`): install the pinned hotato release, verify
  `contracts/`, re-score `fixtures/`, publish the JSON reports plus the
  JUnit file, and fail the pipeline on a regression. Gates are guarded, so
  an empty `contracts/` or `fixtures/` directory stays a green starting
  state. See `docs/CI.md`.

### Changed
- README and all docs rewritten for clarity: point-first headings, one idea
  per sentence, front-door pages readable in under a minute. Every command,
  link, and scope boundary is preserved.
- Emitted paths and the scorer-identity hash use forward slashes on every
  OS, so artifacts and `wheel_hash` are byte-identical across machines.
  POSIX-only tests carry explicit skip reasons on Windows.


## [1.6.1] - 2026-07-14

### Changed
- First run leads with `uvx hotato start --demo` everywhere, so the first
  command works on a machine with an externally managed Python. pip stays
  shown for CI and container images, and pipx or a virtualenv for a project
  install.
- The demo and `contract verify` output reads as a designed surface: a metric
  with no finite value shows `n/a`, times carry their `s` unit, and a bundle
  whose canonical digest matches reads as `integrity: intact`. The stored
  `authenticity` field and schema are unchanged.


## [1.6.0] - 2026-07-14

### Added
- **Transcription cache with verify-by-diff.** Transcripts are cached
  content-addressed (audio bytes + model + device + compute type + language +
  options); a hit replays the byte-identical stored transcript.
  `--no-transcribe-cache` re-runs the model fresh and surfaces any drift as a
  diff beside the cached baseline.
- **Silero VAD runs directly on onnxruntime.** The MIT-licensed
  `silero_vad.onnx` weights ship inside the package and the `[neural]` extra
  installs onnxruntime + numpy. Segmentation output is equivalence-tested
  against the reference implementation.

### Added
- **Proof-preserving counterexample compiler.** `hotato counterexample compile`
  reduces one failing deterministic scripted scenario to a private runnable
  capsule while preserving a source-selected structured failure branch. The closed
  `hotato.reducers.v1` deletion algebra can earn a replay-verified 1-minimal
  claim or report budget exhaustion without upgrading the claim. Strict
  verification binds the source-to-final transform chain, evaluator source
  digest, result/content/trace identities, derived reports and scripts, and a
  resource-bounded manifest. Current-evaluator reproduction, `git bisect`
  predicate semantics, share-safe export, machine JSON, and MCP parity ship on
  the same local, deterministic path. The scripted proof lane covers 15
  assertion kinds through 48 closed, schema-coupled failure branches.

### Security
- **Counterexample capsules refuse bounded integrity violations and excessive
  proof work under the v1 snapshot model.** Private inspection binds the
  embedded source, oracle, target identity, every proof artifact, and canonical
  human projection. Share-safe capsules use an exact member allowlist and
  canonical reports. Manifest members, streaming
  journals, JSON depth/number handling, scripted evidence, and proof-regex work
  have explicit limits before replay. Minimality verification refuses more than
  512 accepted transformations and 512 remaining deletion units. Accepted
  steps require fresh evaluations, and proof-lane regexes exclude variable
  quantifiers so an untrusted no-match search cannot introduce backtracking
  amplification.

### Fixed
- Failure identity distinguishes missing evidence from mismatched values across
  tool arguments/results, state, entities, and termination attributes; ordering
  distinguishes an absent step from one present before the accepted prefix;
  latency distinguishes a declared measurement from the simulator default.
  Minimization cannot delete the decisive evidence and retain a coarser branch.
- Coarse reduction descends into free-form mock tool arguments and results, so
  unrelated payload entries can be deleted while the keyed mismatch survives.
- Share-safe reports expose a closed payload-free failure code while omitting
  source fields, keys, rule names, reducer paths, and runnable inputs.

## [1.5.4] - 2026-07-14

Documentation and packaging refresh.

### Documentation
- README and docs streamlined for scannability, and optimized to render cleanly
  on narrow / mobile screens: wide tables restructured into readable lists,
  long code lines wrapped, dense diagrams normalized. The README (which is also
  the PyPI project page) states verified-Linux CI support plainly.

### Build & CI
- A Ruff lint gate (import-sort, pyflakes, bugbear) runs in CI as a green
  ratchet, plus an MCP tool-registration smoke job.
- Release builds run through a pinned, hash-checked composite build action for
  reproducible sdists and wheels.


## [1.5.3] - 2026-07-13

Data-loss and security hardening from a continued independent audit.

### Security
- **Deleting a recording never destroys another workspace's evidence.**
  `delete_recording` previously unlinked the shared content-addressed blob with
  no reference check, so deleting one workspace's recording could remove a blob
  another workspace still referenced (same digest via dedup). It now revokes
  only the caller's pointer and removes the shared bytes ONLY when no workspace
  still references the digest; the content store validates canonical digests and
  refuses paths that escape the store (symlink fan-out), and legal hold blocks
  pointer revocation. Registry roots remain the authority; CAS lineage is never
  an ACL.
- **Outbound requests stop leaking credentials.** A redirect to a different
  origin (scheme, host, or port, including an https->http downgrade) now strips
  `Authorization`, `Cookie`, and `Proxy-Authorization` and re-runs SSRF/private-
  target validation; and URLs are sanitized (basic-auth, query tokens, signature
  params, fragments removed) before appearing in any error message, log, or CLI
  text.
- **Recording/media downloads are bounded.** A shared bounded reader refuses a
  declared-oversize body before reading and streams in fixed chunks, so peak
  memory no longer scales with response size across the download call sites.

### Fixed
- Canonical JSON rejects non-finite floats (NaN/Inf) instead of emitting them.
- Every MCP tool result and error carries the uniform control envelope
  (evidence_status / refusal_reason / artifact_digests / pending_irreversible_action).
- Redaction validation rejects empty/reversed/negative/overlapping spans, a
  same-file alias, a no-op, and truncated PCM with a clean error (no crash), and
  reports the requested spans.
- Query-token stripping percent-decodes keys, so an encoded token key no longer
  leaks its value.
- `test run --out <dir>` no longer fails when the home directory is uncreatable.

### Changed
- The README banner is compact enough to render without clipping on a mobile
  viewport.


## [1.5.2] - 2026-07-13

Security and correctness hardening from an independent validation pass.

### Security
- **Evidence reads are authorized by live workspace registry roots, not content
  presence.** `hotato serve`'s `/evidence/<digest>` previously served any blob
  present in the shared content-addressed store, so a digest acted as a
  cross-workspace capability: an authenticated request could read another
  workspace's evidence by digest, and a blob orphaned by deleting its
  workspace's only live root stayed readable. Every evidence read now requires
  the digest to be reachable from a live registry root scoped to the caller's
  workspace (a direct reference edge, or one hop through a rooted manifest's own
  declared artifacts -- registry authority, never CAS lineage), and returns an
  identical 404 for unreachable and absent digests so no existence oracle
  remains.
- **Share-safe Failure Records reject embedded Windows-absolute paths.** The
  scrubber and validator now recognize drive-letter (`C:\...`), UNC
  (`\\server\...`), and embedded/mixed-separator absolute paths anywhere in a
  string, closing a local-filesystem-identity disclosure under the
  `share-safe-v1` profile.

### Fixed
- **Capability routing** rejects an arbitrary caller `contract_uri` (only the
  documented Hotato spec URI or none), pairs a missed addressed bid with a false
  trigger only when both share the same battery and configuration, and degrades
  an under-labelled addressed event to `engagement_control` instead of silently
  returning no requirement.
- **Reference-run artifacts are byte-reproducible.** The conversation manifest
  timestamp resolves deterministically (an explicit value, then
  `SOURCE_DATE_EPOCH`, then a fixed epoch) instead of the wall clock, so two
  seeded runs produce byte-identical conversation artifacts.
- **Voice Failure Atlas**: the publication gate rejects drive/UNC/backslash
  paths OS-independently, stored render transcripts match the CLI exactly, and
  attribution is footer-only.
- **Fleet**: a trial is approvable only with an approval-eligible verdict above
  the evidence floor (a refused, evidence-tier-zero experiment can no longer be
  approved), and `discover` binds the resolved recording id so a contract can be
  created from the ingested call.
- **MCP**: the server reports hotato's application version (not the transport
  SDK's), and the tool inventory is a single canonical source shared by the
  server, docs, and tests.
- **CLI machine output**: `capture --format json` and `start` route progress
  lines to stderr so stdout is a single parseable JSON object, and
  `start --demo --dir` creates a missing nested output directory (still refusing
  a path that exists as a non-directory).
- **Neural provenance**: a `--backend neural` run labels the non-reference
  backend in the event JSON (additive `vad_backend`); the default energy path
  emits no such key and stays byte-identical.
- **Cold-path evidence** redacts absolute paths recursively and separates
  deterministic artifacts from timestamp-bearing ones, so the committed proof
  reproduces from the released wheel and leaks no local paths.
- **Packaging/metadata**: the SBOM declares its coverage scope explicitly,
  `docs/CI.md` pins one canonical current-release example, and
  `requires-python` carries a documented support policy.

### Changed
- The reference reference-agent kit and shipped shell scripts are pinned to LF
  line endings, so a normal Windows checkout no longer rewrites digest-pinned
  evidence to CRLF and invalidates its byte oracle.
- `hotato init` machine-JSON locators are normalized to `/` on every platform.


## [1.5.1] - 2026-07-13

### Changed
- **PyPI links resolve.** The PyPI long description is now `README.pypi.md`,
  generated by `scripts/build_pypi_readme.py` from `README.md` with every
  repo-relative link rewritten to an absolute GitHub URL, so links on the PyPI
  page resolve instead of 404ing against pypi.org. A staleness test keeps it
  current and asserts no repo-relative link remains.
- **The default Action gate makes zero package-index requests.** In `action`
  mode the pinned checkout now runs directly off `PYTHONPATH` (gate.py invokes
  `python -m hotato`), so there is no pip step, no isolated build backend
  fetched, and no index contact -- the executed code is exactly the pinned
  revision. The exact-version pin mode still installs from PyPI, its documented
  intent. `docs/CI.md` shows the canonical full-commit-SHA pin.


## [1.5.0] - 2026-07-13

The Voice Failure Atlas and the delta prove-and-stop gate (deltas D5, D6).

### Added
- **Voice Failure Atlas builder** (delta D5): a
  deterministic, static, server-rendered site generator
  (`scripts/build_atlas.py`, stdlib-only) built from typed sources under
  `atlas/{records,contracts,implementations}/`, schema-validated against three
  new schemas (`hotato.atlas-record.v1`, `hotato.atlas-contract.v1`,
  `hotato.atlas-implementation.v1`). Every seeded record embeds a real
  `hotato.failure-record.v1` projection and a verbatim CLI transcript, both
  produced by actually running `hotato contract create` / `verify` / `record
  render` against the bundled share-safe `examples/funnel-demo/` fixtures --
  never invented. The builder computes every capability verdict by calling
  the real D3 router (`hotato.capability_routing.route_capability`); a typed
  source never carries a pre-baked routing outcome. A hard publication gate
  (schema+digest validity, evidence traced, release/consent/license present,
  share-safe profile, safe relative paths) decides what is indexed, and the
  bundled funnel-demo battery's own paired evidence demonstrates the
  backchannel-exclusion rule end to end: the "yielded to an addressed
  backchannel" record routes to `turn_intent_discriminator`, and the
  addressee-gate-eligible pattern class ships as an honest, unindexed,
  zero-record stub since no cleared non-addressed-speech fixture exists yet.
  Two builds from the same sources are byte-identical. `tests/test_atlas.py`
  (27 tests, including a gate check that an `origin=fixture` record's
  cited fixtures resolve to shipped files under `examples/`).

- **Cold-path proof** (delta D6): `scripts/cold_path_proof.py` records
  share-safe evidence that a clean install of the released package runs the
  credentialless offline first-run (`hotato start --demo`) with no key and
  zero intervention. Every recorded string is path-redacted and the
  deterministic evidence is byte-identical run to run, so a clean rerun
  reproduces it. The two human cold batteries (unfamiliar engineers; hosted
  starts) are recorded as un-fabricated and never filled with invented counts.
  `tests/test_cold_path_proof.py` asserts the path-hygiene and reproducibility
  invariants.

## [1.4.1] - 2026-07-13

Supplied interaction labels and the capability router that reads them (deltas
D2, D3), plus the consumer Action hardened to install and gate on itself.

### Added
- **Interaction labels v1** (`hotato.interaction-label.v1`, delta D2): optional,
  backwards-compatible metadata about one event -- `speech_presence`,
  `addressed_to_agent`, `floor_intent`, `label_authority`, `label_ref`. Every
  field is supplied by a human, a trusted source, or a marked fixture; Hotato
  never derives addressee or turn intent from timing, energy, transcript, or a
  model verdict. An event with no label reads as all-unknown, so existing
  artifacts stay valid. Stdlib-only validator, cross-checked against the JSON
  Schema; the never-infer guarantee is asserted in tests.
- **Capability routing v1** (`hotato.capability-requirement.v1`, delta D3): a
  pure, deterministic router that reads SUPPLIED interaction labels on a paired
  addressee-control battery and routes to the narrowest capability the paired
  evidence supports (a missed addressed floor bid plus a false trigger on the
  opposite risk), or to no recommendation at all. It never infers addressee or
  intent, never reads audio, names no vendor, and emits a provider-neutral
  verdict: capability id, evidence refs, acceptance tests, the input-health
  causes it checked and cleared, and an optional neutral contract URI.
  Insufficient or untrusted labels route to `engagement_control` with the
  missing axes listed. Seven routing fixtures + validator-checked schema.
- A root composite GitHub Action (`action.yml`): a repository with no hotato
  source runs a committed suite, conversation test, or contract verification,
  gets a five-lane job summary on pass and on failure, reads artifact paths
  from step outputs, and gates on hotato's own exit code. The default run
  installs the pinned Action revision itself with `--no-deps` (no package
  index, no model, no ASR, no Node tool, no external judge) and needs only
  `permissions: contents: read`; artifact upload stays an explicit consumer
  step. A conformance fixture (`tests/fixtures/action-consumer/`) plus a
  local harness (`tests/test_action_consumer.py`) cover pass, mixed-fail,
  inconclusive, absent-lane, advisory-unavailable, malformed, and
  path-with-spaces cases against recorded machine results, and the
  `action-smoke` CI job runs the consumer shape against the local checkout.
  Consumer usage: docs/CI.md, "The root Action".

## [1.4.0] - 2026-07-13

The Failure Record and the conformance-for-PRs foundation.

### Added
- **Failure Record v1** (`hotato.failure-record.v1`): a content-addressed,
  five-lane record projected from a test-run, suite-run, or contract-verify
  result. `hotato record render SOURCE[#TEST_ID] --out DIR` renders it to
  deterministic JSON, Markdown, inert self-contained HTML, and a 1200x630 SVG,
  all carrying the same record id. Outcome claims cite tool/state evidence;
  transcript-only outcomes are refused. Safe projection excludes raw audio,
  transcript bodies, payload values, secrets, environment values, and absolute
  paths by default.
- **Consumer GitHub Action** (root `action.yml`): an unrelated repository can
  pin it by commit SHA to run a committed Hotato suite/test/contract and get a
  five-lane job summary (with the reproduction command and acceptance-check
  ids) on pass and failure, artifact and exit outputs, and hotato's own gate
  status as the step exit. Read-only permissions; the default gate installs no
  model, ASR, Node tool, or external judge.
- **`hotato regression prepare`**: turn one confirmed failure into a sanitized,
  deterministic, committed regression bundle on disk (rights and redaction read
  from versioned metadata files). It prepares files locally and stops; it never
  uploads, commits, opens a pull request, or changes an agent.
- **Failure record viewer in `hotato serve`**: a read-only `/records` list and
  detail view over the workspace records, token-gated and path-contained, with
  an explicit empty state.

## [1.3.3] - 2026-07-13

First-run truth, paste-safe guidance, and a workspace that opens itself.

### Fixed
- The demo contract is built from the declared missed interruption, selected by
  evidence fields (never candidate rank). The prior positional selection could
  present an unscorable stop-without-caller event as the demo failure, with a
  talk-over of 0.0s contradicting the printed story; the selected event now
  verifies scorable with a measured talk-over. Zero or ambiguous matches raise
  loud internal-contract errors instead of shipping the wrong moment.
- Every example command hotato prints is runnable as-is on one line, across the
  demo next steps, setup/init scaffolds, scan/fixture/loop/investigate hints,
  and all help epilogs: angle-bracket placeholders became concrete values or
  bracket-free names, backslash line-continuations were joined, and inline
  shell comments moved to prose. A guard test now walks the guided surfaces
  and the full help tree to keep them paste-safe.

### Changed
- hotato serve opens the browser to the working token-carrying URL on start
  (--no-open or a non-interactive terminal skips it), prints that URL
  prominently, and greets a token-less visit with a short landing page that
  explains how to get in. The five views were polished; access stays
  token-gated and read-only.
- README, docs, and the CLI first-run screen state capabilities from strength:
  boundary claims were rewritten as what each mechanism does, with genuine
  scope limits kept and stated plainly.

## [1.3.2] - 2026-07-13

First-run experience, docs, and metadata.

### Added
- A first-run screen for `hotato` (run with no subcommand): an ASCII logo, the
  one command to try (`hotato start --demo`), and the browser workspace
  (`hotato serve`). Color is optional and gated on an interactive terminal
  (respects NO_COLOR), it degrades to plain text when piped or on a narrow
  terminal, and every command shown is copy-paste-safe. Still zero runtime
  dependencies.

### Changed
- README leads with an ASCII logo hero; the banner image moves alongside the
  five-dimension section. Rebuilt for clarity and organic reach: pain-led hook, the per-dimension
  scorecard surfaced high, a dedicated CI-gate section, and a corpus-contribution
  path; skimmable structure.
- Revamped repository banner and added a social-share card, matched to the
  hotato.dev brand and the conversation-QA positioning.
- PyPI metadata: a pain-led Summary, expanded keywords, full trove classifiers,
  and richer project URLs (Documentation, Changelog, Bug Tracker).
- CONTRIBUTING and the issue/PR templates reframed to conversation QA with a
  five-minute first-contribution on-ramp and the corpus loop front and center.

## [1.3.1] - 2026-07-13

A pre-launch hardening pass driven by an adversarial audit of the 1.3.0 wheel:
every user-killer, security hole, and broken-quickstart found in a fresh install
is closed here. No schema or API change; every fix is behavior- or copy-level.

### Fixed
- **CI gate no longer greens on a judge that could not run.** `hotato rubric run
  --gate` and `hotato test run --gate-judge` now exit non-zero when the judge
  backend ERRORs (down/unreachable Ollama, an exception), not only on a FAIL --
  a judge that did not run is not a pass, and a gated exit 0 must mean the check
  ran. INCONCLUSIVE (the model ran but abstained) stays advisory;
  suites gate it via `inconclusive_policy`. Advisory (no `--gate`) is unchanged.
- **`hotato start` no longer prints raw `None`.** A missing decision-margin,
  time-to-yield, or talk-over value renders as `n/a` (matching the rest of the
  CLI) instead of `Nones` / `None` on the "top candidate" line.
- **Credential-safe redirects on the config-inspect path.** `hotato inspect`
  (`inspect_vapi` / `inspect_retell`) now installs the hardened opener before
  sending its `Authorization: Bearer <vendor key>` request, so a cross-host
  redirect can no longer exfiltrate the API key -- closing the one credentialed
  HTTP path that had missed the SSRF-safe opener the judge and state adapters
  already use.
- **Copy-paste-safe machine hints.** Every `--format json` "next" command
  (`fixture create`/`promote`, `contract create`, `init webhook`/`starter`,
  `loop`, `investigate label`) shell-quotes interpolated paths, so a workspace
  path with a space no longer breaks the exact command hotato tells an agent to
  run.
- **`hotato start` drops two unfinished flags.** `--stack` / `--folder` were
  stubs that printed "not yet in this build"; they are removed from the flagship
  command. Use `hotato sweep` / `hotato analyze` to score a live stack or folder.
- Self-host docs: the local-model-judge and compose commands in SELF-HOST.md are
  corrected to the real CLI contract and run as written; the seeded demo now
  carries data across all five scorecard dimensions.
- `hotato simulate` has a working first-run path from a bare install (a packaged
  example / `--init`), a docs/SIMULATE.md quickstart, and clearer scenario errors.

### Changed
- README and llms.txt lead with the always-works `pip install hotato && hotato
  start --demo` (the `uvx` form is offered as a zero-install alternative), and
  now surface the full 1.3.0 conversation-QA surface -- the five-dimension
  scorecard and the test/suite/rubric/simulate/serve commands -- so the front
  doors describe the product the release is named for.
- Reduced authenticity-protest and "honesty wall" branding across user-facing
  copy in favor of plainly stating what each mechanism does.

## [1.3.0] - 2026-07-13

The Conversation QA Foundation. Hotato evolves from a turn-taking analyzer into an
open-source, self-hosted conversation-QA system for voice agents: simulate, evaluate,
review, and track calls across five dimensions -- outcome, policy, conversation, speech,
reliability -- with the evidence behind every result. Deterministic checks stay
structurally separate from a model-judged rubric lane; there is no blended score.

### Added
- **Conversation Test + Conversation Artifact.** `hotato.conversation-test.v1` is the
  primary unit -- one file defining a caller goal + facts + behavior, environment,
  expected tools, deterministic and rubric assertions, and repetitions. `hotato.conversation.v1`
  is the canonical evidence artifact binding audio, transcript, trace, evaluations, review,
  and provenance by digest; `hotato conversation verify` re-hashes every child and refuses on
  tamper. `hotato test run <conversation-test>` evaluates one call end to end into an artifact
  plus a per-dimension scorecard.
- **The five-dimension scorecard.** Results group by dimension (outcome / policy / conversation
  / speech / reliability), each with its own pass/fail/inconclusive counts -- never a merged or
  blended number, structurally forbidden in every schema.
- **State-grounded outcomes (Authority 1 & 2).** Twelve new deterministic assertion kinds
  (tool_result, tool_error, state, state_change, handoff, dtmf, termination, latency,
  timing_contract, entity_accuracy, sequence, count) read the authenticated trace or a post-call
  state adapter -- never the agent's spoken claim. An LLM verdict is structurally unable to
  satisfy a state/tool_result assertion.
- **Real state adapters.** `HttpStateAdapter` + `SqlStateAdapter` query a system of record
  (injection-safe parameterized SELECT-only; unreachable state -> INCONCLUSIVE, never a guess);
  network paths are egress-opt-in. `docs/STATE-ADAPTERS.md`.
- **Rubric / local-model judge (`hotato rubric`).** Model-judged assertions run against a real
  local model (Ollama by default -- zero egress) or an opt-in hosted endpoint, with pinned model
  digest, content-addressed verdict cache (byte-identical replay, `--no-cache` drift diff),
  citations, and a human-review queue. Advisory by default; `--gate` opts into CI failure.
  Structurally separate schema/shelf from the deterministic layer. `hotato[judge]`. `docs/RUBRIC.md`.
- **Deterministic simulation at scale.** `scenario.v1` + `hotato simulate` render a scripted
  caller into a byte-stable simulated conversation (origin=simulated, never conflated with real;
  bad sims marked SIMULATOR_INVALID). `hotato simulate --matrix` expands a variation matrix and
  runs it in parallel, reproducible across worker counts, with pass@1/pass@k/pass^k reliability
  (Wilson CI).
- **Drive-a-call.** Real Twilio (TwiML scripted caller) and Vapi call origination wired into
  `run_scenario`, credential- and egress-gated; recordings flow into the existing capture path.
  `docs/DRIVE-A-CALL.md`.
- **Self-hosted team workspace (`hotato serve`).** A local web application over the fleet
  registry with five views (release readiness, scenario matrix, conversation inspector, failure
  clusters, production health), token auth, an append-only audit log, read-only, zero external
  calls. `docs/WORKSPACE.md`.
- **Suites & releases.** `hotato suite run` executes a suite of conversation-tests into the
  registry and gates on the suite's `inconclusive_policy`; `hotato release compare A B` gives
  digest-exact per-dimension deltas and new-vs-fixed. Plus `hotato scenario init/validate`.
- **Self-host deployment.** A container + `docker compose` (optional local Ollama judge profile)
  that runs the whole platform in your own cloud/VPC with zero external calls on the default path,
  plus a zero-egress verification script and the zero-migration promise. `docs/SELF-HOST.md`.
- **Conversation QA Foundation reference agent + benchmark.** A 375-run reference agent
  (25 scenarios x caller behaviors x environments x repetitions, offline) and a five-part
  benchmark proving simulation validity, outcome-grounding, assertion determinism, report
  reproducibility, and failure-to-regression promotion. `examples/reference-agent/`.
- **`inconclusive_policy`** (report | fail | refuse) so a suite can make INCONCLUSIVE gate CI.
- **Unified report** now carries timing + transcript + trace + assertions + reliability + the
  conversation-artifact provenance in one file.

### Changed
- **Positioning.** Hotato is now the open-source, self-hosted conversation-QA system for voice
  agents. The deterministic turn-taking engine is the crown-jewel Conversation dimension within
  the suite, not the whole product. README, CLI help, `__init__`, llms.txt, server.json, CITATION,
  and pyproject unified to one definition (guarded by a positioning-lockstep test).
- The 8-entity data model (agents / releases / suites / scenarios / runs / conversations /
  evaluations / reviews + assertion_runs) migrated additively into the fleet registry,
  concurrency-safe.

### Security
- The rubric judge HTTP paths install the hardened, credential-stripping redirect opener (with
  the SSRF re-check) before any request, so a hosted-judge endpoint cannot exfiltrate the API key
  via a cross-host redirect.

### Fixed
- Concurrent `Registry()` construction on a fresh database no longer deadlocks (the CI-hang class),
  via autocommit + retry + `INSERT OR IGNORE`.
- Reliability data supplied without an assertions envelope is never silently dropped.
- The source distribution now imports on Python 3.9-3.11 (two 3.12-only f-strings
  rewritten) and ships the full self-host deployment bundle (Dockerfile,
  `docker-compose.yml`, `deploy/`), so `pip download hotato` extracts to a complete,
  buildable self-host tarball.

## [1.2.0] - 2026-07-12

### Added
- **`hotato investigate` -- one call to a CI-ready contract.** Takes a local
  dual-channel WAV or a provider `--stack`/`--call-id` and, in one guided flow,
  pulls or opens the audio, authenticates the capture origin
  (frozen-regression / provider-pulled / operator-asserted-local), runs the
  channel-eligibility gate, ranks candidate moments, then `investigate label
  <ref> --expect yield|hold` mints a signed label-record and builds a portable
  signed contract plus the exact CI verify and recapture commands. Reuses only
  shipped primitives; never fabricates a label or a verdict.
- **`hotato assert` -- a deterministic assertion DSL.** Five no-model, offline,
  byte-stable assertion kinds over the conversation: `phrase` (regex on the
  transcript, with absent/compliance mode), `pii` (ssn/card-Luhn/email/phone
  detectors with a `must_not_leak` gate and a redacted-transcript artifact that
  never echoes the raw value), `policy` (named, versioned offline rule packs),
  `tool_call` (name/args/order/count/never-before checked against the
  `voice_trace.v1` spans, not the transcript), and `outcome` (task success as
  all-of/any-of deterministic predicates). Every result carries its `kind` and
  `deterministic` flag; the summary splits deterministic vs judge counts and by
  construction emits no blended `overall_score`. Embeddable in a contract so
  `contract verify` gates on a failing assertion, reported separately from the
  timing verdict. New `docs/ASSERTIONS.md`.
- **Signed label-records + Ed25519 signing (`[sign]` extra).** A human label is
  now a signed `label-record.v1` bound to the exact audio, not an inference; the
  opt-in `[sign]` extra adds asymmetric Ed25519 attestation, with HMAC kept as a
  separately-named shared-secret tier.
- **Opt-in `[transcribe]` extra (faster-whisper).** A transcript layer that is
  context only and never alters the timing score.
- **Copy-paste CI configs** for GitLab, Jenkins, Azure DevOps, and CircleCI in
  the docs (alongside the existing GitHub Actions gate).
- **`--notify URL` webhook on `sweep` and `hotato fleet run`.** Opt-in,
  repeatable, off by default: when the run finishes it POSTs one JSON summary
  (counts, the top candidate moments -- id, kind, timing numbers only -- and
  local artifact paths, plus a one-line `text` field a Slack incoming webhook
  renders directly) to each URL. No audio, no credentials, no transcript text
  ever leaves the machine through it. A non-http(s) URL is refused before any
  network attempt (exit 2); once sent, delivery is fail-open -- a down or slow
  webhook logs one stderr warning and never breaks the run. New module
  `src/hotato/notify.py`; documented in `docs/EGRESS.md` and the README.
- **`hotato fleet trend`.** Reads the local fleet SQLite registry and writes
  one self-contained HTML page: per-agent talk-over and time-to-yield trend
  lines (p50/p95 per day), candidate moments discovered over time, and
  experiment outcomes (improved/inconclusive/refused). Offline, zero external
  assets, hand-rendered inline SVG in the same house style as the sweep
  dashboard. A day with no measurements gets no point; a series with fewer
  than two days of history is reported plainly as "not enough history to
  trend" rather than a faked or interpolated line. New module
  `src/hotato/fleet/trend.py`; documented in the README.

### Changed
- **HTML report template: deduped boilerplate, no scoring change.** The same
  determinism/reproducibility/ceiling/no-accuracy-score story used to be
  restated 4-5 times across a header line and three footer paragraphs; it is
  now one Method line. The stamped out-of-scope negation bullet list (no
  speaker ID, no diarization, no STT, no emotion) is now one link to the
  canonical explanation. The thresholds table is now a single collapsed
  `<details>` block rather than an always-open section. The analytics rollup
  now renders after the per-event cards it aggregates (previously before them),
  and only once a page has at least 3 events. Every measured number and event
  datum on the page is unchanged; this is layout and copy only.
  `src/hotato/report.py`, `docs/REPORTS.md`.

### Fixed
- **Fleet registry: concurrent construction no longer deadlocks (was a CI hang
  on Python 3.11/3.12).** Two threads or processes opening a `Registry` on the
  same fresh `fleet.db` at once raced on the schema-init writes: `PRAGMA
  journal_mode=WAL` needs a brief exclusive lock and is not covered by the
  connect busy timeout, so the loser could raise `database is locked`, and the
  check-then-seed of the `meta` schema_version row could raise a `UNIQUE`
  `IntegrityError`. The connection now opens in autocommit
  (`isolation_level=None`) so the manual `BEGIN IMMEDIATE` in
  `JobQueue.enqueue`/`claim` is the sole, version-stable transaction control --
  removing the implicit-transaction interaction whose timing changed across
  CPython 3.11/3.12; the idempotent init sequence retries locked/busy with
  bounded backoff; the schema_version seed is now `INSERT OR IGNORE` and the
  additive column migration tolerates a concurrent duplicate add. `JobQueue`
  writes retry the same way. Batch candidate reclustering
  (`FleetAPI.recluster_agent`) now wraps its per-candidate rewrite in an explicit
  `BEGIN IMMEDIATE`..`COMMIT`, so it stays all-or-nothing under autocommit instead
  of committing each row individually. The concurrency regression test's worker
  threads are now daemons, so any future regression fails fast instead of a leaked
  non-daemon thread hanging the run. `src/hotato/fleet/registry.py`,
  `src/hotato/fleet/jobs.py`, `src/hotato/fleet/api.py`,
  `tests/test_fleet_jobs_concurrency.py`, `tests/test_fleet_recluster_atomicity.py`.

### Security
- **Evidence kernel hardened (external review).** A forged, altered, or
  wrong-key manifest/attestation signature can no longer reach the signed tier
  (it is refused, never silently downgraded); the scorer pin now covers the real
  scorer bytes and refuses on mismatch; a suspected channel swap or crosstalk
  yields advisory candidates but a null verdict and a refused contract until the
  mapping is confirmed. A temporal precommit + replay ledger binds recapture
  receipts. The 19-finding robustness audit is closed and the FIFO/blocking-open
  hang class is eliminated with an AST lint that fails CI on any new unguarded
  external open.

## [1.1.1] - 2026-07-11

Documentation consistency patch.

### Fixed
- Corrected the MCP tool count in the README and llms.txt: the MCP server
  exposes the `voice_eval_run` scorer plus eleven fleet tools (twelve total);
  the README/llms.txt still said "eight fleet tools" after three read tools
  (`candidate_inspect`, `experiment_status`, `experiment_create`) were added in
  1.1.0. `docs/MCP.md` was already correct.
- Added a release-guard test that fails on any wrong spelled tool count in a
  human doc, not only the "one tool" undercount.

## [1.1.0] - 2026-07-11

Guardian/Fleet build-out. Completes the self-hosted control plane over the
evidence kernel per the definitive overhaul plan: the automatic
failure-to-fix loop, capture-receipt attestation, batch discovery, privacy
controls, and an honest trust headline are now wired end to end. Always
recommends, never auto-deploys.

### Added
- **Bounded experiment engine.** `hotato fleet experiment propose` generates a
  catalogue-driven, bounded variant set (baseline + lower/higher/adjacent/
  two-parameter, capped ~6), each with an expected-effects block stated before
  execution; `FleetAPI.experiment_run_all` runs each as a manifest-bound clone
  trial and Pareto-ranks the eligible ones over visible components (no blended
  "Hotato score"). A unified, versioned typed parameter catalogue backs it.
- **Capture-receipt attestation is emitted, not just verified.** The clone
  runner signs a per-fixture capture receipt bound to the trial nonce + decoded
  PCM; with a signing key present a clean fresh recapture now reaches the
  ATTESTED tier ("PAIRED FRESH-RECAPTURE IMPROVED"). Without a key it stays
  operator-asserted, never silently upgraded.
- **Batch discovery + cross-call clustering.** `hotato fleet run` ingests and
  discovers a set of recordings, clusters candidate shapes across calls to fill
  the recurrence / novelty / covered-by-contract ranking components, and
  advances a durable watermark. Never auto-labels.
- **Idempotent jobs.** ingest / discover / experiment now record leased,
  idempotent jobs, so a duplicate webhook, scheduler retry, or worker crash
  converges on one logical result.
- **One-click contracts + high-stakes.** `hotato fleet contract create
  --from-candidate` labels a reviewed candidate and mints + registers a real
  failure contract; `--high-stakes` marks it, making the canary gate and the
  private benchmark's high-stakes counts real.
- **Privacy controls, wired.** `hotato fleet retention` attaches a retention/
  consent policy; `hotato fleet delete` removes audio and leaves a durable
  deletion receipt (a legal hold blocks it); `hotato fleet redact` produces a
  DERIVED redacted copy (new PCM hash + parent lineage), never the original
  evidence. Reports gained an audio-reference mode that references audio by
  hash instead of inlining PCM.
- **`hotato fleet experiment approve`** records a human approval decision
  (recorded only; never deploys). **`hotato synth`** generates deterministic
  synthetic perturbations of a real recording as a SEPARATE synthetic axis.
- **Optional label-suggestion review assistant** (plan §12): abstains on any
  uncertainty and is advisory only; a human label is always required to promote
  a contract.
- **MCP** gained `candidate_inspect`, `experiment_status`, and
  `experiment_create`; every fleet-tool response now carries a uniform
  evidence-status / refusal / artifact-digest / pending-action envelope.
- **New registry entities** (contract sets, deployment receipts, attestations,
  variants, watermarks, per-recording retention/PII) and a signed trial
  manifest (wheel hash, adapter identity, required yield/hold lists, capture
  plan).
- **Supply chain**: SBOM generation (per-extra profiles) wired into release CI,
  build-provenance attestation, and a reproducible-build hash check; PyPI
  Trusted Publishing documented as the default path.

### Changed
- **Trust headline is honest under leakage.** Cross-channel leakage now cautions
  whenever the suspected leaked component would alter the receiving channel's
  activity mask (onset / active frames / talk-over), closing a gap where a
  verdict-changing leak still read clean. "safe to scan" is now
  "eligible for scan" (eligibility, not a safety guarantee).
- **Claim contracts.** A machine-readable evidence-language table governs public
  claim phrases; cards fail closed if a rendered claim exceeds the evidence tier
  it earned, and the copy lint enforces the same table.

## [1.0.1] - 2026-07-11

Evidence-integrity hotfix. An external audit of 0.10.0 found several paths where a
green artifact could be produced from an input that was not what it claimed. Each
is now closed and covered by a release-blocking adversarial test
(`tests/test_audit_evidence_integrity.py`):

### Fixed
- **Contract media binding.** A signed contract stayed `authenticated` after its
  bundled `audio/event.wav` was replaced (fail could become pass). The bundled
  clip's raw and decoded-PCM hashes are now bound into the signed subject and
  rechecked at `contract verify`; a swapped recording is `tampered`,
  not authenticated, and not passing.
- **Capture receipts are verified.** `fix trial` / `fleet experiment` no longer
  treat any nonempty receipts object as runner-attested. Each receipt is verified
  against the after-side decoded PCM and bound to the trial id + nonce; a missing,
  forged, wrong-key, wrong-trial, or wrong-PCM receipt is refused, never a silent
  downgrade to green.
- **Operator-asserted is not fresh recapture.** A paired before/after whose
  recapture origin is only operator-asserted renders a qualified headline and no
  green accent; the "fresh-recapture" green is reserved for a runner-attested,
  signed, hold-guarded result.
- **Fleet requires a real improvement and honors `--min-n`.** `experiment run`
  now uses the same fail-closed comparison `fix trial` uses: at least `--min-n`
  previously-failing fixtures, at least one now passing, no regression. An
  all-pass-before/all-pass-after battery is inconclusive, never improved.
- **Precommitted trial manifests.** New `hotato fleet experiment create` pins the
  complete fixture universe from the committed battery before any after-side
  capture; `experiment run --manifest <digest>` consumes it and refuses a
  before/after that drops a fixture, so the universe cannot be cherry-picked to
  the results.
- **Opposite-risk (hold) guard.** A yield-directed improvement with no
  previously-passing hold guard is disclosed on the headline ("no hold guard
  submitted") and cannot reach the attested tier; a hold guard that regressed
  caps the evidence at none.
- **Fleet label integrity.** Label ids derive from the full candidate id (two
  candidates from one recording no longer collide and overwrite a human
  decision); a label for a nonexistent candidate is rejected (exit 2).
- **`start --stereo` shows no verdict before a human label.** An unlabeled
  candidate reports only raw timing (onset, frame, boundary sensitivity); a
  yield/hold verdict appears only after `--label`.
- **Adapter capability honesty.** Adapters no longer advertise `run_scenario` /
  `capture_result` capabilities that raise `NotImplementedError`; discovery
  distinguishes "needs credentials" from "not implemented."
- Documentation and test-collection consistency (MCP tool inventory; installed
  `pytest` entry point).

## [1.0.0] - 2026-07-10

First stable release. The evidence kernel and the Guardian/Fleet control plane are
feature-complete, adversarially audited, and fix-verified; the envelope, contract,
evidence-vector, trial-manifest, capture-receipt, and attestation schemas are
stable and additive. Same surface as 0.10.0, promoted to 1.0.0 to commit to the
public API. External, independently-attested proof is not yet published; the
evidence a Hotato artifact claims is always bounded by its inputs.

## [0.10.0] - 2026-07-10

### Added
- **Guardian/Fleet.** A private, self-hosted control plane over the evidence kernel.
  `hotato fleet` (init/agent/ingest/discover/review/label/experiment/status): a
  workspace-scoped registry with no product-level agent cap, a content-addressed
  artifact store with lineage, an idempotent leased job queue, and a Guardian loop
  that recommends but never auto-deploys.
- **Recompute-from-audio proof gate.** `fix trial` pins an immutable trial manifest
  (scorer + one policy + the complete fixture universe + per-fixture onset/expect/
  stimulus) and re-derives every verdict from on-disk audio. It refuses verdict
  tampering, same-PCM re-encodes, dropped fixtures, and unrelated-stimulus swaps; a
  green paired proof requires the evidence tier to reach PAIRED.
- **Evidence vector + lattice.** The public tier of any artifact is the weakest tier
  its required dimensions allow (a minimum over an inspectable vector, never a blended
  confidence percentage). Cards, reports, CLI, and JSON consume it.
- **Capture receipts** (machine-verified fresh recapture vs operator-asserted) and
  **contract authenticity** (canonical digest + detached attestation; a repacked
  loosened-policy bundle is reported tampered, not authenticated).
- **Boundary sensitivity** per event (onset frame, effective onset, decision margin,
  boundary_sensitive), derived in hotato's layer with the vendored engine kept
  byte-identical to upstream.
- **Honest trust headline**: any verdict-changing warning forces "scan with caution";
  explicit three-state input health; leakage judged against the receiver's VAD gate.
- **Evidence-tier-aware rendering + SVG accessibility**; **deterministic synthetic
  perturbations** (kept on a separate axis from real evidence); **privacy/retention/
  redaction**; a **stack adapter capability protocol** with an offline mock loop.

### Changed
- Standalone `verify` on envelope-only input now reads as an unverified envelope
  comparison, never a green paired proof.

### Release integrity
- Version lockstep now gates llms.txt, server.json, and CITATION.cff; offline SBOM
  generator; all GitHub Actions pinned by immutable SHA.

## [0.9.0] - 2026-07-10

### Added
- **`hotato trust` now measures cross-channel leakage and low signal level, two
  input defects that previously passed silently as "safe to scan".** A red-team
  found that symmetric echo bleed at roughly `-40 dB` flips a downstream timing
  verdict (talk-over 400 ms to 1050 ms in the reproduced case) while `trust` still
  reported `scorable: true`, `safe to scan`, and no warning, because crosstalk was
  judged only by whole-clip echo coherence -- a single best-lag cosine that
  unrelated activity elsewhere in the call dilutes below the bar.
  - A new `crosstalk_risk.leakage_db` (with `leakage_direction`) reports the level
    of a consistent, attenuated, delayed COPY of one channel found on the other.
    It is measured from the per-frame level ratio of the copy, which a real leak
    holds constant across every frame the source speaks, so it survives the leak
    being loud enough to re-trigger the other channel's own detector -- exactly
    the regime that breaks the verdict and the one coherence misses. At or above
    `-40 dB` (calibrated to the reproduced break point) the copy is flagged
    (`crosstalk_risk.suspected`), a warning is raised, and the recommendation is
    downgraded to `scan with caution`. The threshold was benchmarked against every
    clean dual-channel fixture in the corpus (12 real recordings and 7 synthetic):
    none carries a consistent copy above `-50 dB`, so none is flagged, and all
    still read `safe to scan`.
  - A `signal level very low` warning is raised when even the loudest channel
    peaks below `-30 dBFS`, the band where turn timing can be under-measured
    downstream while every scorability gate still passes.
  - Both are additive and DISCLOSURE-ONLY: `scorable`, the not-scorable reason
    chain, and the exit code are unchanged. `trust` adds signals and states its
    limits; it never silently changes what passes. `schema_version` stays `"1"`.
    See `docs/TRUST.md`.
### Changed
- **`hotato fix trial`'s provenance guard now verifies identity, it does not
  trust the envelope.** An external red-team of 0.8.0 showed the guard trusted
  the `audio_provenance` JSON: a digest string was compared for equality and
  nothing else, so a hand-written envelope (valid-looking digests, no audio),
  a non-hex or absurd-metadata block, a header-only byte flip (decoded audio
  unchanged), a cherry-picked after set, or a frozen hold could all still reach
  `improved`. The guard is rebuilt around one rule: an `improved` verdict is
  never reachable on unverifiable evidence.
  - **Validate before trusting**: every `sha256` / `pcm_sha256` must be 64-char
    lowercase hex; each side's `sample_rate` / `num_samples` must be plausible;
    the top-level digest must be consistent with the per-side digests it claims
    to combine. A malformed block is UNKNOWN (`inconclusive`), never "a distinct
    recording".
  - **Recompute when the audio is present**: the raw and decoded-PCM sha256 are
    recomputed from the file next to the envelope at trial time; a digest that
    disagrees with the bytes on disk is `refused`
    (`refusal_kind: recompute_mismatch`).
  - **Unverifiable is never `improved`**: a well-formed identity hotato could
    not recompute (the audio was not present) downgrades to `inconclusive` with
    the reason that a fix claim requires provenance hotato can recompute. This
    closes the decisive forgery -- hand-written envelopes with no files.
  - **Compare DECODED PCM, not raw bytes**: identical decoded audio before vs.
    after is the same conversation re-scored (`refused`), so a header-only edit
    or a trailing-byte append can no longer disguise a re-score as a recapture.
    When a side records no `pcm_sha256` the check falls back to the raw digest
    AND marks the fixture unverified.
  - **Completeness**: every before target and hold must have an after
    counterpart; a required only-before fixture is `refused`
    (`refusal_kind: incomplete_after`) with the omitted list.
  - **Holds get the same guard as targets**: a still-passing hold with frozen
    (re-scored) audio is now `refused`, not silently accepted.
  - **`--min-n` is echoed in every surface** (text, JSON, HTML) so a lowered
    floor is always visible.
  - `hotato verify`'s envelope rollup additively reports
    `unpaired.only_before_required` (before targets / holds dropped from the
    after set); the flat `only_before` / `only_after` lists are unchanged.
  - Docs (`docs/FIX-TRIAL.md`, `docs/RECAPTURE.md`) updated to describe the
    hardened guard exactly. No new claim is made about attacker-proofing an
    offline tool a user fully controls; the guard makes the honest-but-motivated
    failure modes impossible or loud, recomputes what can be recomputed, and the
    report states exactly what was and was NOT verified.
- **The apply receipt now renders beside the verdict, and a nested `CLAIM`
  can no longer read as a pass under a red parent.** The same red-team found
  two remaining honesty gaps in the rendered report itself, independent of
  the provenance guard above:
  - **Apply receipt**: `hotato fix trial` never calls `apply.create_clone`,
    so `apply_dry_run` is always `True` and `apply_created` /
    `apply_applies_change` are always `False`, on every verdict including
    `improved` -- but the rendered report never said so; a green trial from
    an unapplied patch looked identical to one where the change was known to
    be live. `apply_dry_run` / `apply_created` / `apply_applies_change` /
    `apply_receipt_note` are now top-level JSON fields (not just nested
    inside `apply`), a text line right under the header, and pills plus a
    header sentence in the HTML `<header>` block, on every run.
  - **No positive claim under a failed parent**: `hotato verify`'s nested
    `CLAIM` line inside a fix-trial report could read `CLAIM: ... This
    improvement COINCIDES with your change` even when the outer fix-trial
    verdict was `inconclusive` or `refused` (a provenance or completeness
    issue downgraded the outer verdict, but the inner verify claim was
    unaware of it) -- a cropped screenshot of just that block looked like a
    clean pass. `verify.render_text` now takes an optional `superseded_by`
    verdict; `hotato fix trial` passes its own verdict whenever it is not
    `improved`, and a claim that would read "supported" is tagged `CLAIM
    (SUPERSEDED BY {VERDICT})` with a one-line restatement, in both text and
    HTML.
  - **Docs**: `docs/FIX-TRIAL.md` and `docs/RECAPTURE.md` each gained a
    "What this does not stop" section: fabricated-but-freshly-captured
    stimuli, a repacked contract with a loosened policy (manifest integrity
    is not authenticity), resample/codec/gain transforms of the same call
    (a known PCM-identity residual), and that signatures are not
    implemented. None of this is new attacker-proofing; it is stating
    plainly what a green result does and does not establish.

## [0.8.0] - 2026-07-10

### Fixed
- `hotato connect` crashed on Windows with `AttributeError: module 'os' has
  no attribute 'fchmod'` when storing credentials. `os.fchmod` is POSIX only;
  `tempfile.mkstemp` already creates the file owner-scoped, so the call is
  now guarded. Found the same hour by the new cross-OS CI matrix, on its
  first run.


### Added
- **Fresh-capture provenance guard for `hotato fix trial`**: every run
  envelope now records an `audio_provenance` block per event (a streamed
  sha256 of the exact audio bytes scored, plus sample rate and frame count;
  additive, `schema_version` stays `"1"`). `hotato fix trial` compares this
  identity, before vs. after, for every fixture the `improved` claim rests
  on: an identical digest (the after run re-scored the SAME recording the
  before run scored, just under a different threshold) downgrades the
  verdict to `refused` (the same exit code `3` `hotato apply`'s own refusal
  uses); a digest missing on either side (an older envelope, or one built by
  hand) downgrades to `inconclusive`, never assumed fresh. Distinct, known
  digests proceed exactly as before. This byte-identity guard blocks literal
  byte-for-byte reuse of the same recording passed off as a fix; it does not
  establish conversational freshness, authenticate envelope contents, pin
  scoring policy, or detect a transformed export of the same call -- a
  header-only edit or trailing-byte append still produced a distinct raw
  digest at this stage and could pass. (Closed in `[0.9.0]` below by
  comparing decoded PCM, not raw bytes.) See
  `docs/FIX-TRIAL.md#fresh-capture-provenance-guard-a-re-score-is-never-a-fix`
  and `docs/RECAPTURE.md#how-hotato-tells-a-recapture-from-a-re-score`.
- **Report-facing claim-language cautions**: `hotato contract verify` now
  prints, in every text and HTML render, "This result re-measures stored
  evidence. It does not test the current agent." `hotato fix trial`'s
  audio-provenance section now prints a matching provenance/revision
  caution wherever that section renders (an `improved` verdict, or a
  `refused`/`inconclusive` one the fresh-capture guard itself downgraded).
  Both strings are drawn from a new claim-language table -- which evidence
  you have, what it accurately lets you say, and the common overclaim it
  does not support, for five levels of evidence from a historical contract
  alone up to a production rerun after deploy -- and new audio-handling
  rules covering what raw call audio may contain, why redaction does not
  remove spoken content, and an audio-free evidence summary to prefer when
  sharing proof without the recording. See
  `docs/RECAPTURE.md#claim-language-what-each-kind-of-evidence-lets-you-honestly-say`
  and `SECURITY.md#audio-handling`.

## [0.7.2] - 2026-07-10

### Fixed
- `hotato contract pack` refuses a bundle containing any symlink (file or
  directory) instead of silently following it. A planted link could
  previously ship bytes from OUTSIDE the bundle inside the archive (for
  example a linked secret file). A packed bundle is now self-contained by
  construction; copy real files into the bundle instead of linking them.
  Found by external diligence probing the published packages.

## [0.7.1] - 2026-07-10

### Fixed
- `hotato contract unpack --force` no longer deletes the existing output
  directory before the archive has proven valid. In 0.6.0 and 0.7.0, a
  corrupt or hostile archive combined with `--force` destroyed the
  directory the user asked to replace and then failed. The destination is
  now touched only on the success path, after every guard and the full
  extraction into the temporary directory have passed. Found by external
  diligence against the published package; regression-tested with the
  exact reproduction.

## [0.7.0] - 2026-07-09

### Added
- **`hotato explain`, root cause by layer without guessing**: reads the
  evidence a run already produced (score results, fix plans, trust reports,
  contract bundles, attached voice traces) and emits an attribution per
  failure: the likely layer (turn-taking taxonomy: failure layer, type,
  confidence, fixability, opposite risk), the evidence for and against it,
  the explicit unknowns (for example, no client-side playout trace attached),
  and one safe next action. When the evidence cannot support an attribution
  (an unlabeled candidate, a not-scorable input, conflicting signals) it
  REFUSES with the reason instead of guessing. Text, `--format json`
  (schema `hotato.explain.v1`), and HTML report output. Explain reports
  timing evidence; it does not prove root cause and does not infer intent.
- **`hotato fix trial`, one command from candidate fix to before/after
  proof**: composes the shipped primitives (`apply --clone`, the verify
  battery, contract verification, and the opposite-risk guardrails) into a
  single fail-closed trial: baseline on the recorded failure, apply the
  candidate change in a clone (production config is never touched), re-run,
  and check neighbouring and opposite-risk cases. Verdicts: improved
  (exit 0), regressed (exit 1, forced by ANY fixture or opposite-risk
  regression even when everything else improved), inconclusive (exit 3,
  refused rather than softened). The report embeds the explain attribution
  for the failure under trial.
- Operator-grade capture depth for LiveKit and Pipecat in the starter
  templates and docs: where turn-taking configuration lives in each stack
  and how to capture per-party audio for scoring.

### Fixed
- `hotato contract unpack` now treats every archive as hostile input:
  rejects path traversal (including backslash and drive-letter forms),
  symlinked and encrypted members, duplicate member names, members not
  declared in the manifest, oversized decompression (512 MiB default,
  `--max-bytes` / `HOTATO_CONTRACT_MAX_UNPACK_BYTES` override, enforced
  against actual streamed bytes, not just declared metadata), and
  compression-ratio bombs. Extraction stays atomic: a refused archive
  leaves nothing behind. `pack` cross-platform byte determinism is now
  explicit rather than incidental.

## [0.6.0] - 2026-07-09

### Added
- **`hotato contract create/verify/inspect/pack/unpack`, the portable failure
  contract**: turns one real call moment into a self-contained `<id>.hotato/`
  bundle -- `contract.json` (schema `hotato.contract.v1`), the (clipped) audio,
  frame-level timing evidence, an input-health (trust) report, a shareable SVG
  card, a CI policy, and the exact replay/CI commands. `create` wraps the SAME
  round-trip scorability guarantee `fixture create` gives (via
  `--from-candidate FILE#N`, `--stereo`, `--caller`+`--agent`, or the opt-in
  `--mono --diarize` path): a not-scorable moment or a mono recording is
  refused with the reason (exit 2) and no bundle is written; the
  diarized-mono path never silently upgrades an indicative-only verdict, and
  reports frame-level evidence as unavailable rather than fabricating
  it. `verify` re-scores a contracts directory (or one bundle) against each
  contract's own recorded policy and is the CI gate: exit 0 every contract
  passes, 1 a regression, 2 a usage error; emits text/JSON/HTML/JUnit.
  `inspect` prints one contract. `pack`/`unpack` round-trip a bundle through
  one deterministic single-file `.hotato.pack` archive with a sha256 manifest
  checked on unpack (a corrupted or tampered archive is refused, exit 2,
  nothing partial left behind). `hotato card` also renders a contract
  directly (kind `voice-turn-taking-contract`). Redacted by default (a
  candidate ref / source recording name is hidden unless
  `--include-identifiers`); no call ids, paths, or transcript text in any
  artifact. New module `hotato.contract`, new schema
  `schema/contract.v1.json`, docs `docs/CONTRACTS.md`.
- **`hotato trace ingest/attach/export`, the voice-trace observability
  bridge**: `ingest --otel FILE --out voice_trace.jsonl` parses either a
  standard OTel JSON export (`resourceSpans`, best-effort span/event
  flattening) or hotato's own documented OTel bridge JSONL into schema
  `hotato.voice_trace.v1` (caller/agent audio activity, TTS cancel/stop, ASR
  partials, tool calls, ...). `attach BUNDLE --trace voice_trace.jsonl`
  writes the trace into `<bundle>/traces/voice_trace.jsonl` and re-renders
  `evidence/timeline.html` with the trace's events drawn as a scale-aligned
  row, reading the bundle's OWN `evidence/frames.jsonl` and `contract.json`
  back in -- it never re-runs the VAD or diarizer, so it works on a
  diarized-mono bundle (no frame-level evidence) without the diarization
  extra installed, noting the missing base timeline instead of
  fabricating one. `export BUNDLE --format otel --out FILE` writes the
  attached trace back out as the same bridge JSONL `ingest` reads, so
  `ingest -> attach -> export -> ingest` round-trips the identical spans.
  The evidence page states findings plainly -- "Evidence suggests TTS
  cancellation delay: cancel requested at 2.60s, audio stopped at 2.90s
  (delta 0.30s)." -- always followed by "Hotato does not prove root cause."
  and an explicit "Unknowns: no client-side playout trace was attached."
  line. Redacted by default (call id, agent id, ASR transcript text). New
  module `hotato.trace`, new schema `schema/voice_trace.v1.json`, docs
  `docs/TRACE.md` and `docs/OTEL.md`.
- **`hotato run --mono call.wav --diarize`, the opt-in, quality-gated
  mono-scorability front-end**: a single-channel (mixed) recording -- until now
  the hard coverage wall, rejected as not scorable -- becomes scorable by running
  an off-the-shelf speaker diarizer over the mono to recover who was active when,
  reconstructing two caller/agent tracks, and feeding the EXISTING dual-channel
  scorer (zero engine edit). The two-channel path stays the gold reference; this
  widens coverage, honestly labeled. A pluggable **diarizer-backend seam**
  (mirroring the neural-VAD seam) ships three backends behind `--diarizer`:
  `pyannote` (local, offline, CPU-viable, default; `[diarize]`), `sortformer`
  (NVIDIA NeMo, local/GPU, best self-hostable on telephone;
  `[diarize-sortformer]`), and `pyannoteai` (HOSTED, best absolute, requires
  `--egress-opt-in`; `[diarize-hosted]`). A **real per-file confidence gate**
  (extending `trust.py`'s scorability model) reads six signals -- speaker count,
  per-speaker activity, mean segmentation posterior, embedding cluster-separation
  margin, overlap ratio, segment churn -- into a `separation_confidence` and a
  tier: `high` scores normally (labeled `diarized-mono`), `low` scores but stamps
  `indicative_only` and fires NO SLA gate, `refuse` is not scorable (exit 2).
  Caller/agent assignment reuses the floor-dominance heuristic to PROPOSE a
  mapping stated as an assumption, overridable with `--caller-speaker` /
  `--agent-speaker`; a balanced mapping downgrades to indicative. On this path
  `signals.echo` / crosstalk is definitionally N/A (two slices of one microphone)
  and the echo gate never fires. `hotato trust --stereo mono.wav --diarize`
  reports the separability tier (high/low/refuse) WITHOUT scoring. No silent
  fallback anywhere: a missing extra / token / model raises a clean
  `BackendUnavailable` (exit 2) and NEVER scores raw mono. Honesty invariants:
  the default path (no `--mono`/`--diarize`) stays byte-identical (a mono file is
  still rejected as today), and a diarized-mono verdict is never presented as
  equivalent to a true dual-channel measurement. New module `hotato.diarize`
  (`DiarizationResult`, the backend registry, `reconstruct_tracks`,
  `separation_confidence`, `assign_speakers`, `prepare_diarized_mono`, the
  pyannote/sortformer/pyannoteai adapters, and a hermetic stub backend); new
  `--mono`/`--diarize`/`--diarizer`/`--caller-speaker`/`--agent-speaker`/
  `--egress-opt-in` flags on `hotato run` and `--diarize`/`--diarizer` on `hotato
  trust`; documented in `docs/DIARIZE.md`. The `LIMITS.does_not_do` /
  `METHODOLOGY.md` "no diarization" framing is superseded: two channels is the
  gold reference; mono is scorable via the opt-in, quality-gated front-end,
  labeled indicative below the bar. Speaker IDENTIFICATION is still out of scope
  (a diarizer assigns anonymous SPEAKER_00/01; it never says who a person is).
- New optional extras `[diarize]`, `[diarize-sortformer]`, `[diarize-hosted]`
  (the `[diarize]` path raises the effective Python floor to >=3.10; the stdlib
  core stays >=3.9). Dependency licenses are logged in `docs/DIARIZE.md` and
  carried in the envelope's `diarization.licenses` block (pyannote-audio MIT;
  community-1 weights CC-BY-4.0; Sortformer streaming v2 CC-BY-4.0 -- the offline
  v1 is CC-BY-NC and is never shipped).
- **`hotato card INPUT[#REF] --out card.svg`, a shareable card from any hotato
  result**: renders a DETERMINISTIC, stdlib-only, 1200x630 SVG with NO external
  resource (no font, image, stylesheet, script, or link; all color inline), so
  it drops straight into a PR, an issue, or a slide and looks the same forever.
  Four kinds are auto-detected: a **talk-over** candidate and a **false-stop**
  candidate (from a `sweep`/`analyze` candidate ref `FILE#N`), the
  **threshold-funnel** fix plan (`decision: do_not_tune_single_threshold` -- the
  hero card: "NO SINGLE THRESHOLD CAN FIX THIS", `fix class: engagement-control`),
  and a supported **verify** rollup ("FIX VERIFIED WITHOUT BREAKING
  BACKCHANNELS"). Every card names the MEASURED timing moment and never a verdict
  about intent, and carries no accuracy number. **Redacted by default**: a call
  id, a filesystem path (only a basename is ever shown), and a vendor recording
  name are hidden unless `--include-identifiers`. Exit 0 written / 2 bad input.
  New module `hotato.card`; three commit-ready assets under
  `docs/assets/cards/` regenerated by `scripts/render_card_assets.py`; documented
  in `docs/CARDS.md`.
- **`hotato start --demo`, the guided, credential-less first run**: one command,
  no account, no network. It sweeps the two bundled real demo calls, writes the
  sweep result (`hotato-sweep.json`), a self-contained HTML dashboard
  (`hotato-sweep.html`), and the threshold-funnel card
  (`hotato-no-single-threshold.svg`), then turns one real missed-interruption
  candidate into a demo failure contract (`contracts/demo-missed-interruption.hotato`,
  `--expect yield`) and verifies it immediately -- it fails, so the
  loop is visible end to end: a real failure becomes a
  candidate, becomes a portable contract, and `contract verify` catches it.
  It then prints the exact next commands: promote a candidate into a
  permanent fixture, run those fixtures in CI, re-verify the demo contract,
  and render a card. The `--stack`/`--folder`/`--stereo` modes are
  placeholders in this build and route to `hotato sweep`/`analyze`/`run`.
  Exit 0 done / 2 usage. New module `hotato.start`; documented in
  `docs/START.md`.

### Fixed
- **`hotato.diarize` pyannote 4.x compatibility**: `Pipeline.from_pretrained`
  now tries the `token=` kwarg first and falls back to the removed
  `use_auth_token=` name on `TypeError`, and a new `_unpack_pipeline_output`
  branches on the `DiarizeOutput` object pyannote.audio >=4.0 returns from a
  pipeline call (previously unpacked as a 3.x `(Annotation, embeddings)`
  tuple, raising `TypeError`/`AttributeError` against a 4.x install) so both
  pyannote 3.x and 4.x load and score cleanly. `_embedding_margin` no longer
  divides by a zero norm and fabricates `cos = 0` (read by the confidence
  gate as adequate separation) for a degenerate (zero-norm / non-finite)
  speaker centroid; it now returns `None` -- "no margin available" -- the
  same no-signal result a missing embeddings array already gave.
- **New yield-boundary confidence gate signal (`signals.yield_boundary`)**:
  benchmarked against a real pyannote community-1 backend over the AMI
  corpus (in-repo harness: `tools/bench_diarize/`, dev-only, never shipped),
  the existing six diarization-quality signals measured clean, well-separated,
  stable speakers but were anti-correlated with verdict correctness -- the
  `high` confidence tier reproduced the dual-channel `did_yield` verdict
  LESS often than `low`, concentrated in short-yield, backchannel, and
  sub-second talk-over cases, and present even at DER 0.000, because the
  verdict turns on a sub-250 ms agent-quiet gap that DER's collar and the
  quality signals forgive. The new 7th signal replays the engine's yield
  logic directly over the diarization timelines (no model calls, no
  reconstruction), perturbs the speaker boundaries by +/- 0.25s, and checks
  the verdict survives; a yield resting on a briefer-than-0.5s caller run
  (backchannel-grade) or that flips under the boundary nudge is barred from
  the `high` tier (drops to `low`, indicative-only) even when the other six
  signals look clean. Honest `high` coverage on real material shrinks as a
  result -- that is the point: `high` now requires a boundary-robust
  verdict, not just clean diarization. The embedding-margin signal itself
  measured uninformative on the AMI benchmark (clustered ~0.43-0.52
  regardless of verdict correctness); left as measured, redesign deferred to
  a future gate-recalibration stage rather than tuned blind here.

## [0.5.0] - 2026-07-09

### Added
- **`hotato trust --stereo call.wav`, the input-health "trust doctor" you run
  BEFORE scoring a call**: it inspects one recording and reports whether the
  audio is even SCORABLE, so a bad export (a mono file, a silent channel, a
  swapped channel map, a hot capture) is caught up front instead of producing a
  confident-looking but meaningless turn-taking verdict downstream. The report
  covers per-channel activity (caller expected on channel 0, agent on channel
  1), a possible channel-swap flag (the channel mapped as the caller holding the
  floor far longer than the one mapped as the agent, the reverse of the usual
  pattern), sample rate, duration, clipping (per-channel peak dBFS and full-scale
  fraction), leading silence, crosstalk risk (cross-channel echo coherence), and
  the three scorability checks (separated tracks, enough caller activity, enough
  agent activity), then recommends `safe to scan` or `NOT SCORABLE` with the
  specific reason AND the next step (e.g. `caller channel has no detected speech`
  -> `verify channel mapping or export dual-channel again`). Three input defects
  are not scorable and exit `2` (mono, identical channels, a silent required
  channel); clipping, long leading silence, crosstalk, and a possible swap are
  non-blocking warnings. By construction it NEVER labels intent and NEVER emits a
  turn-taking verdict (no yield/hold, no pass/fail): it answers exactly one
  question -- is this audio good enough to score? Offline, stdlib-only, and
  reuses the existing hardened WAV reader, reference framing, energy VAD, and
  cross-channel echo coherence (no DSP is reimplemented). `--format json` emits
  one agent-parseable report (`kind: "input-health"`; branch on `scorable`, read
  `not_scorable_reason` / `next_step` on a defect). New module `hotato.trust`
  (`trust_report`, `render_text`); new `trust` subcommand in `hotato.cli`;
  documented in `docs/TRUST.md`. Exit codes: `0` safe to scan, `2` not scorable
  or usage error.
- **`hotato apply PATCH_JSON --clone --name NAME`, the guarded, clone-only
  staged apply -- the one command that can mutate external platform state, and
  the most conservative in the codebase**: it reads a `hotato patch` artifact and
  either PRINTS the fresh staging clone it would create (the default, fully
  offline dry run) or, only with `--yes` and credentials, creates a NEW staging
  assistant that is the source config with the patch applied. Five rules hold by
  construction. (1) CLONE-ONLY: there is no production-apply path; a non-`--clone`
  invocation is a clean usage error (`production apply is not supported; use
  --clone to apply to a fresh staging assistant`), and nothing ever `PUT`/`PATCH`es
  the source (the one writing call is a `POST` that creates a NEW assistant).
  (2) REFUSAL-FIRST: a both-axes threshold-funnel patch
  (`do_not_tune_single_threshold`) is REFUSED before anything, printing the exact
  vendor-neutral recommendation (`No config patch will be applied` / `Reason: both
  missed real interruption and false stop on backchannel, one threshold cannot
  safely fix both` / `Recommended: enable or add engagement-control /
  backchannel-aware turn detection`) and exiting a distinct, documented code (`3`)
  so a script can tell "refused by design" apart from a usage error -- the refusal
  is the feature. (3) OPPOSITE-RISK REQUIRED: apply refuses unless `--battery`
  carries BOTH a yield and a hold fixture, so a fix is never applied blind.
  (4) GATED SIDE EFFECT: the default dry run prints exactly the clone it would
  create and the patch it would apply, touching no network; only `--yes` with
  credentials reaches the platform, and the create is the only networked function
  (`apply.create_clone`) -- it reads the source (`GET`), applies the patch to a
  copy, and creates a NEW assistant (`POST`), never mutating the source. (5) NAME
  REQUIRED. Clone-appliable stacks are vapi (`POST /assistant`) and retell (`POST
  /create-agent`); LiveKit/Pipecat keep config in source, so apply points at the
  source edit from `hotato patch`. New module `hotato.apply` (`build_apply`,
  `apply_patch_to_config`, `build_clone_config`, `create_clone`,
  `battery_classes`); docs/APPLY.md; the `apply` subcommand and its `Exit codes:`
  epilog + `hotato describe` manifest entry.
- **`hotato verify --policy hotato.verify.yaml`, the anti-bandaid gate that
  fails a fix which moves one axis while regressing (or never testing) the
  other**: a policy file declares two things and BOTH must hold for verify to
  pass. `target.improve` is the success criteria (the failure the fix set out to
  move) -- a signed number is a required delta, so `talk_over_sec_p95: -0.5`
  means the pooled talk-over p95 must drop by at least 0.5s, and a keyword
  (`decrease`, `increase`, `no_worse`, `no_better`, `unchanged`) states
  direction, so `failed_count: decrease` means fewer fixtures may fail.
  `guardrails` are HARD fail conditions: `max_new_false_yields` and
  `max_not_scorable` cap what a threshold bandaid would silently trade in, and
  `require_hold_fixture` / `require_yield_fixture` refuse to certify a battery
  that does not even test the opposite axis. verify exits 1 unless EVERY
  guardrail holds AND EVERY target is met, so a patch that cuts talk-over by
  making the agent yield to everything meets the talk-over target but trips
  `max_new_false_yields` on the hold fixtures and the whole check fails. The
  guardrails and targets are shown in the `verify.html` proof (a `Policy check:
  PASSED`/`FAILED` headline plus an ok/violated and met/unmet table) and in the
  text and JSON output, reading ONLY the numbers `verify_sides` already measured
  -- nothing is re-scored. The report still states COINCIDENCE, never causation.
  The policy is parsed with the standard library alone (Hotato's core carries no
  third-party runtime dependency, PyYAML included), over the small documented
  subset the shipped `examples/verify-policy/hotato.verify.yaml` uses; an
  unknown key, a wrong-typed value, an empty policy, a tab indent, or a list is
  a clean exit-2 usage error, never a silent misread. Supported target metrics:
  `talk_over_sec_p95`, `seconds_to_yield_p95`, `failed_count`,
  `false_yield_count`. New: `verify.load_policy`, `verify.evaluate_policy`.
- **`hotato verify --out verify.html`, the flagship fix-verification proof as
  one self-contained offline HTML report**: a `.html`/`.htm` path passed to
  `hotato verify --out` now writes a single, zero-external-asset page (any other
  extension keeps writing the proof JSON, unchanged). The page reuses the
  report/analyze house style and reads ONLY the numbers `verify_sides` already
  measured; it re-scores nothing. Its headline is `Fix verification:
  PASSED`/`FAILED`, tied to the SAME honesty bar the text/JSON claim enforces (a
  battery-scale claim needs `--min-n` previously-failing fixtures AND at least
  one now passing AND no regression, so a low-n or regressed battery never earns
  a PASSED stamp). A TARGET section shows the failure it set out to move
  (pooled talk-over p95 and the failing-fixture count, before to after) and an
  OPPOSITE-RISK section shows what a naive threshold bandaid would silently
  break (hold/backchannel fixtures and the false-yield count, before to after,
  with the new-false-yield and not-scorable guardrails flagged ok/violated), so
  a "fix" that just makes the agent yield to everything is caught. Every paired
  fixture is listed with its machine-stable compare word, and unpaired fixtures
  are reported, never dropped. The conclusion states COINCIDENCE, never
  causation ("Hotato reports coincidence, not causation."), names what the
  artifact does not prove (timing only; no controlled experiment, no cause, no
  semantic-correctness judgement), and the whole page carries no em or en
  dashes. The verdict/target/opposite-risk numbers are derived by a pure
  `verify.verdict_model(v)` and the page by `verify.render_html(v)`.
- **`hotato pr create --fixtures DIR --repo OWNER/REPO --title T [--yes]`, a
  directory of promoted fixtures turned into a pull request that adds them as
  regression tests**: reads a hotato fixtures directory (the `--out DIR` that
  `hotato fixture promote` wrote, with `scenarios/` and `audio/`) and renders a
  plain markdown PR body: a title from the caller, a line per fixture (its id,
  the `yield`/`hold` label a maintainer chose, the call it was promoted from,
  and the clip onset), and the exact `hotato run --scenarios DIR/scenarios
  --audio DIR/audio` command that scores every added fixture. The fixtures are
  described as MEASURED CANDIDATE moments saved as tests, never verdicts and
  never intent. Two boundaries are structural, not prose: the body renderer
  (`prcmd.build_pr`) is a pure, offline function -- it emits the body and the
  exact `git` and `gh` argv it *would* run and touches no network and no
  subprocess (the one filesystem read, loading the scenarios, is isolated in
  `prcmd.load_fixtures` exactly as `issuecmd` isolates its sweep read); and the
  actual side effect runs only from `pr create` AND only under `--yes` with an
  explicit `--repo`. The default is a dry run that prints the body and the exact
  commands (cut a feature branch, stage the fixture files, commit, push, `gh pr
  create`) and changes nothing. Two safety invariants hold even under `--yes`:
  the change lands on a NEW feature branch (`hotato/<title slug>` by default,
  never the default branch directly, refused if it is a protected/default branch
  or equals `--base`) and the push is never a force-push. It reuses the same
  fixture schema `hotato fixture create`/`promote` writes, so the PR that lands
  the fixtures and the command that scores them read the same files. `git` and
  `gh` are required only for `--yes`; a missing binary or a non-zero exit
  surfaces as the standard exit-2 structured error, and a failing git step never
  proceeds to `gh`.
- **`hotato issue create SWEEP_JSON --repo OWNER/REPO --top N --label L [--yes]`,
  a sweep result turned into a confirm-or-ignore GitHub issue**: renders a
  `hotato sweep --format json` (or `hotato analyze --format json`) result into a
  plain markdown issue body with a title from the run, a worst-candidate block
  (call id, time, kind, the measured overlap/gap number, the report it came
  from), and, for each of the top `--top` candidates, a confirm-or-ignore
  section carrying the exact `hotato fixture promote FILE#N` command for BOTH a
  `--expect yield` and a `--expect hold` label plus a close-it line for when the
  moment is not a turn-taking failure at all. The moments are described as
  MEASURED CANDIDATES, never verdicts and never intent. Two boundaries are
  structural, not prose: the renderer (`issuecmd.build_issue`) is a pure,
  offline function -- it emits the body and the exact `gh` argv it *would* run
  and touches no network and no subprocess; and the actual side effect runs only
  from `issue create` AND only under `--yes` with an explicit `--repo`. The
  default is a dry run that prints the body and the exact `gh issue create`
  command and creates nothing, mirroring the project default
  `github_issue_on_candidate = false`: Hotato never opens an issue on your behalf
  unless you ask for it in that exact call. The candidate parsing reuses the
  SAME parser `hotato fixture promote` uses, and the per-candidate promote
  commands and the measured-number headline reuse the SAME renderers the
  sweep/analyze dashboard draws, so a ref in the issue resolves byte-for-byte to
  the ref on the page. `gh` is required only for `--yes`; a missing binary or a
  non-zero `gh` exit surfaces as the standard exit-2 structured error.
- **`hotato init webhook --stack vapi|retell|twilio --target fastapi --out DIR`,
  a generated set-and-forget webhook worker**: scaffolds a small, self-hostable
  FastAPI worker that turns a voice platform's call-ended webhook into a passive
  turn-taking regression monitor. The worker verifies the webhook secret, then
  hands the payload to `hotato ingest` (the same composable primitive) which
  fetches the dual-channel recording READ-ONLY and scans it for CANDIDATE
  moments; it adds no vendor call of its own. It writes a candidate report and,
  when configured, posts a Slack summary and/or a GitHub notification (both off
  by default; it opens no GitHub issue unless you explicitly set
  `HOTATO_GITHUB_CREATE_ISSUES=1` with a repo and token). The scaffold emits
  eight files -- `README.md`, `hotato.yaml`, `app.py`, `requirements.txt`,
  `Dockerfile`, `.env.example`, `.github/workflows/deploy.yml`, and
  `tests/test_webhook_contract.py`. The contract test ships inside the generated
  project and pins the FOUR honesty invariants with an AST scan of `app.py`
  (never a substring match on prose): (1) it never calls a platform
  config-mutation endpoint -- all vendor I/O is delegated to `hotato ingest`, so
  the worker holds no vendor API host; (2) it never labels intent or emits a
  verdict -- the only `hotato` subcommand it may call is `ingest`, never `run`,
  `verify`, `fixture`, `--expect`, ...; (3) it verifies the webhook secret
  before any parse, fetch, or scan (constant-time `hmac.compare_digest`, 401 on
  mismatch, run first in the handler); (4) the recording fetch is read-only.
  Per-stack signature verification and event detection ship as verified template
  fragments under `hotato/templates/webhook/` and render into the worker; only
  stacks with a verified webhook and a read-only fetch are offered. The three
  offered stacks are Vapi (shared-secret `X-Vapi-Secret`; event
  `end-of-call-report`), Retell (HMAC-SHA256 over the raw body in
  `X-Retell-Signature`; event `call_ended`, webhook-driven only since Retell has
  no list endpoint), and Twilio (HMAC-SHA1 over url + sorted params in
  `X-Twilio-Signature`; the `recordingStatusCallback` `completed` status, with
  dual-channel handled read-only by `hotato ingest`). Vapi is the reference
  worker and Retell and Twilio reuse its skeleton, differing only where the
  platform differs (verification, event name/shape, recording-fetch
  channel handling). Scaffolding is offline: no network and no credentials are
  needed to generate. Gated by `tests/test_init_webhook.py`, which asserts the
  eight files land, `app.py` parses (syntax + AST), `hotato.yaml` matches the
  worker schema, and the four invariants hold under an AST scan, for all three
  stacks, and drives each stack's worker end-to-end (a bad secret is rejected
  401 before anything runs, a non-terminal event is ignored, a call-ended event
  invokes only `hotato ingest --stack STACK`).
- **Promote actions on every analyze/sweep dashboard card**: each ranked
  candidate card now carries three actions. "Promote as yield fixture" and
  "Promote as hold fixture" copy the exact `hotato fixture promote
  REPORT_JSON#RANK --expect yield|hold --id SUGGESTED_ID --out tests/hotato`
  command to the clipboard (`navigator.clipboard`, with a hidden-textarea
  `execCommand` fallback for `file://` pages); "Ignore" hides the card on
  the page only, client side, no state, pausing its embedded player first.
  The ref number is the card's own #N rank chip; the suggested id is call id
  + kind + rank, kebab-cased to the same slug rule `fixture create --id`
  enforces; the report-json name is the producing command's DEFAULT json
  result name (`hotato-analyze.json`, `hotato-sweep-STACK.json`), never the
  --out path, so the dashboard stays byte-identical whatever the page was
  saved as, and a caption names that file and how to write it. You pick the
  label; the page never does. No animation anywhere, so reduced-motion needs
  no gating: feedback is an instant text swap. Gated by
  `tests/test_analyze_promote_buttons.py`, which parses the DOM attributes
  (never substring-matches), pins the exact copied payloads, rank refs, and
  slug ids, and runs one copied command verbatim against the json it names.
- **`hotato fixture promote CANDIDATE_REF`, the confirm step of the monitor
  loop**: promote one candidate moment from a `hotato sweep --format json` or
  `hotato analyze --format json` result straight into a permanent regression
  fixture. The ref names the result file and the candidate --
  `hotato-sweep.json#3` (the Nth candidate, 1-based in rank order, the same
  #N the report shows) or `analyze.json#call_abc123:2` (the Nth candidate
  from one call, matched by source path, file name, extension-stripped stem,
  or pulled call id). The candidate carries the recording, the onset, and the
  kind, so no `--stereo` and no `--onset` is retyped; you add the
  `--expect yield|hold` label and promote reuses the exact `fixture create`
  path: the clipped two-channel `DIR/audio/ID.example.wav` plus
  `DIR/scenarios/ID.json`, scored immediately, with a not-scorable candidate
  refused with the reason (exit 2) and partial outputs removed. The
  scenario provenance records `created_by: hotato fixture promote`, the
  candidate ref, and the candidate kind. To make refs resolvable from
  anywhere, sweep/analyze envelopes now also record `folder_path` (the
  analyzed folder's absolute path); a moved result still resolves via
  `--folder DIR`, and an unresolvable recording names every path tried.
  Gated by `tests/test_fixture_promote.py`: both ref forms, end-to-end
  promotes from a real `sweep --demo` result and a real `analyze` result, the
  1-based rank contract, resolution fallbacks, and every refusal path.
- **`hotato sweep --demo`, the zero-setup first sweep**: runs the full
  pull -> analyze sweep pipeline over the two bundled real demo calls (the
  same recordings `hotato demo` scores), with no vendor account, no
  credentials, and no network. The output is a real sweep's output: the same
  HTML dashboard (default `hotato-sweep-demo.html`) and the same
  `--format json` envelope, pull summary block included, produced by the
  identical analyze code path. `--demo` names a source of calls, so combining
  it with `--stack`, `--call-id`, `--since`, `--allow-mono`, `--dir`, or any
  credential flag is a clean exit-2 usage error that names each offending
  flag. Gated by `tests/test_sweep_demo.py`, which runs every test under a
  network guard (`urllib.request.urlopen` plus raw socket connects) and
  asserts the demo envelope's key set equals a real sweep's.

## [0.4.1] - 2026-07-08

### Fixed
- **`hotato describe` and `hotato --version` self-reported "0.3.1" in the
  0.4.0 wheel**: the 0.4.0 release bump missed the `__version__` literal in
  `src/hotato/__init__.py`, so every runtime self-report -- the `--version`
  banner, the `describe` manifest's `version` field (json and text), and
  stackbench's `provenance.hotato_version` -- carried the previous release's
  number. Scores and envelopes were unaffected; only the reported version
  string was wrong. Now gated by `tests/test_version_lockstep.py`, which
  anchors `hotato.__version__`, the `describe` manifest, and installed dist
  metadata against `pyproject.toml` (the previous describe test compared the
  manifest to `__version__` itself, which agreed even when both were wrong),
  and the release checklist enumerates every version lockstep site.

## [0.4.0] - 2026-07-08

### Fixed
- **Softer unsourced wording in `docs/WHY.md` and `README.md`**: "about one in
  five reported barge-in bugs" is now "in our observed reports, many alleged
  barge-in bugs," and "the two highest-frequency complaints" is now "two
  common complaints"; no percentage or frequency ranking is claimed. Same
  treatment for "four timing failures dominate real transcripts" in
  `docs/WHY.md`, now "show up again and again."

### Added
- **`hotato describe`, the generated capability manifest**: `hotato describe
  [--format json|text]` walks `build_parser()`'s own subparsers (including the
  nested `benchmark compare` / `fixture create`) and emits every subcommand's
  name, purpose, argument list (name, type, required, default, help), and
  documented exit codes, plus the tool version and the two schema URLs read
  straight from the shipped schema files' `$id`. One call for an agent to
  learn the whole CLI instead of scraping `--help` across ~18 subparsers;
  generated from the live parser, so it can never drift from the real flags;
  pure and deterministic. Alongside it: `hotato doctor --format json` (the
  machine envelope on stdout, `report: <path>` on stderr, mirroring `demo`),
  and a single `_EXIT_CODES` source of truth that templates a uniform "Exit
  codes:" epilog into every subparser (previously only six carried one, with
  inconsistent wording); `describe`'s manifest reads the same table, so the
  help text and the manifest cannot disagree.
- **`hotato patch` / `verify` / `loop`, the closed loop (find -> fix -> prove it
  is fixed)**: three commands that carry a fix plan the rest of the way, with the
  two irreversible decisions kept in human hands. `hotato patch <fixplan.json>`
  renders a plan's abstract `{field, from, to}` into a LITERAL, paste-ready
  artifact per platform: a JSON merge-patch body plus a ready `curl` against the
  platform's real config-update endpoint (Vapi `PATCH /assistant/{id}`, Retell
  `PATCH /update-agent/{agent_id}`), using the exact field names the plan carries
  from fixmap's verified knob catalogue; for LiveKit/Pipecat, whose config lives
  in agent source, it emits the exact constructor-kwarg source edit instead of a
  fabricated endpoint; for an unknown stack it names the knob family and asks for
  a target. For the both-axes `do_not_tune_single_threshold` plan it
  emits NO config patch and prints the vendor-neutral, numbers-free
  engagement-control pointer instead (which fires ONLY on that case, never as a
  generic upsell). HONEST: patch PRODUCES the change; it never applies it to your
  platform and makes no network call (`applies_change` is pinned false).
  `hotato verify --before <old-run> --after <new-run>` gives a battery-scale
  before/after proof after you apply the change and re-capture the failing
  fixtures: "N of M fixtures that used to fail now pass, and K of L hold fixtures
  still pass". It reuses the `hotato compare` taxonomy per fixture (`fixed`,
  `regressed`, `improved`, `worse`, `unchanged`, `still_pass`, `not_scorable`)
  and aggregate's pooled-distribution definitions for the talk-over / time-to-
  yield shift. It reports COINCIDENCE, never causation; refuses the headline
  claim under low n (`--min-n`, default 3) while still printing the per-fixture
  facts; treats an unjudgeable side as `not_scorable` and an unpaired fixture as
  reported, never silently dropped; `--fail-on-regression` exits 1 on a
  regressed/worse fixture. `hotato loop [FOLDER]` is one-command orchestration
  with memory: the first run discovers candidate moments (`analyze` -> `scan` ->
  rank) into `.hotato/loop-state.json`; once you have labeled fixtures with
  `fixture create`, the next run plans a guarded fix and points at `hotato patch`
  then `hotato verify`. It tracks state across runs and NEVER auto-labels (you
  supply every yield/hold intent) or auto-applies. New docs: `docs/FIX-LOOP.md`;
  wired into `hotato describe` and the exit-code epilogs. See also
  `docs/FIX-PLANS.md`.
- **`hotato connect` / `pull` / `sweep`, the connect-once bulk pull-and-analyze
  flow**: `hotato connect <stack>` captures a stack's credentials once, runs a
  lightweight live auth check, and stores them at `~/.hotato/connections.json`
  with file mode 0600 (directory 0700) -- local only, never sent anywhere but
  the vendor's own API, never logged. After connecting, `--stack` and the key
  are optional for `pull`/`sweep` when exactly one stack is connected (they also
  fall back to the stack's environment variable). `hotato pull [--stack X]
  [--since 7d] [--limit 50] [--out DIR] [--allow-mono]` bulk-fetches recent
  recordings by looping the existing single-call `capture` fetch over each
  platform's list endpoint into a local folder; a recording that cannot be
  fetched is a clean per-file skip with its reason, never a crash. `hotato sweep`
  runs `pull` then the same zero-config `analyze` over the pulled folder = the
  flagship "connect once, see every turn-taking problem across all your real
  calls" flow, writing one self-contained offline dashboard. Every list/fetch
  endpoint is used exactly as verified verbatim in the integration spec: Vapi
  `GET /call`, Twilio `GET .../Recordings.json`, Bland `GET /v1/calls`,
  ElevenLabs `GET /v1/convai/conversations`, Synthflow `GET /v2/calls`, Millis
  `GET /call-logs`, Cartesia `GET /agents/calls`. Where the spec marks a
  list-calls endpoint unconfirmed (Retell) or a platform capture-in-your-infra
  (LiveKit, Pipecat), no endpoint is fabricated -- Retell pulls from explicit
  `--call-id` values and the limitation is documented. Platform
  payloads are treated strictly as untrusted data (a malformed payload is a
  clean error, never an action from the payload).
- **`--allow-mono` capture/pull adapters for Bland AI, ElevenLabs Conversational
  AI, Synthflow, Millis AI, and Cartesia (Line)**: each with the exact
  spec-verified list + fetch endpoint and the mono caveat. These
  platforms produce a single combined recording with no documented per-party
  channel, so scoring is degraded and gated behind `--allow-mono` /
  `HOTATO_ALLOW_MONO=1`, labelled indicative only. `capture --stack` now accepts
  these stacks in addition to the original five.
- **`docs/CONNECT.md`** (the connect/pull/sweep recipe with per-stack
  credentials) and a comprehensive rewrite of **`docs/ADAPTER-STATUS.md`**
  from the integration spec: build-now dual-channel (Vapi, Retell, Twilio,
  LiveKit, Pipecat), mono-only `--allow-mono` (Bland, ElevenLabs, Synthflow,
  Millis, Cartesia), not integrable (Deepgram Voice Agent, PlayAI), and
  unconfirmed (Regal, Thoughtly, Sindarin, Grok, and channel-layout edge cases),
  each with the exact verified endpoint and notes.
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
  candidate timing moments you review and label with `hotato fixture create`,
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
  `.github/workflows/hotato.yml`. `llms.txt` is reconciled to the real
  command surface: the commands shipped in 0.3.1 that it was missing
  (`capture`, `setup`, and the rest) plus `ingest` and `describe`, which are
  unreleased until this release (they were previously mislabelled here as
  shipped in 0.3.1), and both schema URLs and the MCP one-liner; new `llms-full.txt`
  concatenates README + every `docs/*.md` + `METHODOLOGY.md` + the envelope
  schema with file-boundary headers, built deterministically by
  `scripts/build_llms_full.py`. New `CITATION.cff`.

### Changed
- **`hotato demo` (and the hosted demo report) now score two real recorded
  calls**: the packaged battery's audio was two synthetic shaped-noise
  renders; it is now two real probe calls against a voice agent running a
  provider's default interruption settings -- a missed real interruption
  (`did_yield` false, 0.25 s talk-over) and a false stop on a soft
  backchannel (yielded 0.34 s in). The battery still fails on both funnel
  axes, fires the funnel pointer, and maps to the same two fix classes; the
  audio under each timeline is the exact scored WAV, and two runs remain
  byte-identical. Provenance, plainly: the recordings were made by the
  project itself, per its own recording runbook -- operator-recorded probe
  calls against a scripted fictional-pharmacy assistant. The only human
  voice is the recording operator's own; the agent side is a synthetic TTS
  voice, so no third-party speaker is present. No real names, numbers, or
  identifiers are spoken, and the clips are released under MIT with full
  per-scenario provenance and attestation carried in the scenario metadata.
  Demo copy reworded off "intentionally bad agent" to the real-call
  framing.

### Fixed
- **Atomic writes for downloads, sweep, and every `--out`**: `capture`'s
  recording download goes through a temp file + `os.replace`, so a local
  write failure after a successful fetch can never clobber a pre-existing
  file at `--out`; `sweep`'s HTML dashboard and every CLI `--out` writer use
  the same atomic helper, so an interrupted run cannot truncate a
  previously-good artifact.
- **JSON NaN safety**: every JSON emitter (cli, capture, ingest, patch,
  inspect) goes through `safe_json_dumps` with `allow_nan=False`, so
  NaN/Infinity can never ship as RFC-8259-invalid bare tokens; a non-finite
  value surfaces as the clean exit-2 usage error, and `patch` rejects
  non-finite `from`/`to`/bounds up front.
- **Loop-state validation**: `.hotato/loop-state.json` is validated on load
  (run/stage/discovery/planning/history field types, plus the stage-specific
  keys the renderer reads), so a well-typed but incomplete or hand-edited
  state file is a clean exit-2 error, never a KeyError.
- **WAV / onset / flag validation**: a `sample_rate=0` header or a malformed
  RIFF sub-chunk is a clean exit-2 error (never a ZeroDivisionError or a raw
  stdlib traceback); identical caller/agent channels are refused everywhere
  (comparing a channel against itself produced a confident, meaningless
  verdict and could mint a bogus permanent fixture); onset validity,
  out-of-range channels, and the global scan flags are validated up front so
  a bad flag is a top-level usage error, never a per-file skip that degrades
  into a false clean "found nothing"; a truncated recording is skipped with
  a reason; and one bad file never aborts an `analyze`/`loop` batch.
  (These landed across five overnight hardening rounds, each fix with a
  regression test; the vendored `_engine` stayed untouched and golden
  envelopes byte-identical throughout.)

### Security
- **Default-deny SSRF IP-block**: every vendor-response download URL in
  `capture` and `ingest` is restricted to http(s), and the resolved host is
  refused when it is loopback, private, link-local (cloud-metadata), or
  reserved -- re-checked on every redirect target. `HOTATO_ALLOW_PRIVATE_URLS`
  is the explicit opt-out. Credential headers are stripped on cross-host
  redirects, so a Bearer/Basic secret is never carried off-domain.
- **MCP input sandbox**: the MCP server's `voice_eval_run` stereo/caller/agent
  input paths are sandboxed (`HOTATO_MCP_INPUT_DIR`, else OS temp / CWD /
  bundled fixtures), mirroring the existing `report_path` sandbox.
- **Ingest sandbox, fail-closed**: `hotato ingest`'s local `recording_path`
  handling requires `HOTATO_INGEST_DIR` and fails closed, so a forged webhook
  payload cannot point the scanner at an arbitrary local file.
- **Scenario ids as safe path segments**: scenario ids are validated (no
  separators, no `..`, not absolute) before any path join in
  `run_suite`/`stackbench`/the bundled-audio fallback, plus a realpath
  containment check under `--audio`, so a crafted scenario pack can no longer
  read (or, via `--embed-audio`, exfiltrate) an arbitrary local WAV.

## [0.3.1] - 2026-07-07

### Fixed
- **Self-consistent source distribution**: the sdist now ships the small,
  non-audio test dependencies (scenario labels, manifests, the corpus
  validator, and the deterministic builders under `corpus/` and `examples/`),
  so an extracted sdist collects and runs the full test suite instead of
  hitting collection errors. The heavy real and rendered audio stays pruned;
  suite and class audio is regenerated deterministically by
  `tests/conftest.py` (seed = sha256(id)) when absent, and the tests that
  depend on absent heavy real audio skip cleanly rather than error.
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
  input is refused with the reason (exit 2) and partial outputs are
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
  is a stack-specific knob with direction and trade-off.
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

[0.9.0]: https://github.com/attenlabs/hotato/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/attenlabs/hotato/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/attenlabs/hotato/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/attenlabs/hotato/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/attenlabs/hotato/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/attenlabs/hotato/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/attenlabs/hotato/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/attenlabs/hotato/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/attenlabs/hotato/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/attenlabs/hotato/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/attenlabs/hotato/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/attenlabs/hotato/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/attenlabs/hotato/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/attenlabs/hotato/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/attenlabs/hotato/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attenlabs/hotato/releases/tag/v0.1.0
