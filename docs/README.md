# hotato docs

Find what broke in your agent calls. Pin it so it never ships again. The
loop: `autopsy` one recording (or `scan` a folder, or pull a platform's
recent calls with `vapi health`), `pin` the incident as a portable failure
check, and `prove` re-runs the stored evidence in CI so it never ships
again. This index maps every doc to the step it belongs to.

New here? Run `hotato autopsy ./call.wav` on one recording
(**[AUTOPSY.md](AUTOPSY.md)**), or start with
**[GETTING-STARTED.md](GETTING-STARTED.md)**. The whole loop on one page:
**[LIFECYCLE.md](LIFECYCLE.md)**.

## The loop: find what broke, then pin it

- [AUTOPSY.md](AUTOPSY.md) - `autopsy`, `scan`, and the `<stack> health` commands: one recording (or a folder, or your platform's recent calls) in, the incident list and health report out; `pin` graduates an incident
- [EVIDENCE-CONTRACT.md](EVIDENCE-CONTRACT.md) - the four-tier evidence policy: what every verdict stands on
- [CONTRACTS.md](CONTRACTS.md) - failure contracts: a portable CI bundle of one call moment
- [CI.md](CI.md) - `prove` and the CI gate: fail the pull request on the pinned evidence, offline
- [GETTING-STARTED.md](GETTING-STARTED.md) - one path from first touch to a CI gate
- [START.md](START.md) - guided first run on the bundled demo data
- [WHY.md](WHY.md) - four timing failures a text-level eval cannot see

## Continuous use

- [CONNECT.md](CONNECT.md) - store a stack's credentials once; feed every hotato command from your platform
- [ADAPTER-STATUS.md](ADAPTER-STATUS.md) - per-stack pull, endpoint, and channel-separation status
- [PRODUCTION-MONITORING.md](PRODUCTION-MONITORING.md) - turn production call events into offline regression candidates
- [WORKSPACE.md](WORKSPACE.md) - `hotato serve` and `hotato console`: the self-hosted team web workspace and the live call console
- [SELF-HOST.md](SELF-HOST.md) - run the full workspace in your own VPC

## Working with audio

- [TRUST.md](TRUST.md) - is this recording even scorable?
- [TRUST-MATRIX.md](TRUST-MATRIX.md) - the input-condition-to-behaviour contract for the trust check
- [TRUST-GALLERY.md](TRUST-GALLERY.md) - eight recordings, eight verdicts, verbatim output
- [DIARIZE.md](DIARIZE.md) - diarize a mono recording to make it scorable
- [TRANSCRIBE.md](TRANSCRIBE.md) - attach a transcript beside the timing score
- [FULL-DUPLEX.md](FULL-DUPLEX.md) - score the moment both sides speak at once

## Reference

- [API.md](API.md) - the stdlib-only scoring core, Python API
- [SDK.md](SDK.md) - the typed Python SDK facade over the CLI
- [MCP.md](MCP.md) - the hotato MCP server and its tools, over stdio
- [METHODOLOGY.md](../METHODOLOGY.md) - how the timing measurement works, end to end
- [THREAT-MODEL.md](THREAT-MODEL.md) - which commands are offline, which reach the network
- [EGRESS.md](EGRESS.md) - every network call site mapped to its command
- [VALIDATION.md](VALIDATION.md) - the three separate jobs hotato is validated on
- [EVIDENCE-PACK.md](EVIDENCE-PACK.md) - the reproducible proof artifacts, ranked
- [GALLERY.md](GALLERY.md) - every image and worked example, each reproducible
- [COMPARE.md](COMPARE.md) - where hotato sits next to broad QA platforms
- [evidence/README.md](evidence/README.md) - the evidence standard: what counts, and ranking
- [case-studies/README.md](case-studies/README.md) - the honesty standard every case study meets

## Lab: the deep toolkit

Every command in this section lives under `hotato lab` (`hotato lab --help`).
The public commands above are durable; the lab surface evolves faster, and
every pre-1.17 top-level spelling keeps working unchanged.

### Investigate and evaluate

- [INVESTIGATE.md](INVESTIGATE.md) - one recording in, ranked candidate moments out
- [ANALYZE.md](ANALYZE.md) - drop a folder, rank and hear the worst moments
- [ASSERTIONS.md](ASSERTIONS.md) - deterministic typed assertions over transcript, trace, and timing
- [RUBRIC.md](RUBRIC.md) - the model-judged rubric lane, scored with a pinned local model
- [EXPLAIN.md](EXPLAIN.md) - root-cause-by-layer attribution from existing results
- [STATE-ADAPTERS.md](STATE-ADAPTERS.md) - ground state assertions in your system of record
- [scenarios/dtmf-verification.md](scenarios/dtmf-verification.md) - verify DTMF reached the far end
- [scenarios/echo-self-interruption.md](scenarios/echo-self-interruption.md) - diagnose self-interruption from echo bleed

### Observe

- [OBSERVE.md](OBSERVE.md) - LLM and voice observability from your OpenTelemetry spans, locally
- [TRACE.md](TRACE.md) - voice traces: the pipeline-event timeline
- [OTEL.md](OTEL.md) - ingest OTel traces into the `voice_trace` span format
- [latency-waterfall.md](latency-waterfall.md) - per-hop latency waterfall from a scored call

### Test and simulate

- [SIMULATE.md](SIMULATE.md) - render a scenario into a deterministic labelled conversation
- [CONVERSATION-TEST.md](CONVERSATION-TEST.md) - one file, one call, a per-dimension scorecard
- [SUITES.md](SUITES.md) - four tiered deterministic corpus suites
- [SUITE-RUN.md](SUITE-RUN.md) - execute a suite, per-dimension report
- [GENERATIVE-CALLER.md](GENERATIVE-CALLER.md) - the bounded caller engine: scripts, graphs, replay
- [DRIVE-A-CALL.md](DRIVE-A-CALL.md) - originate a call against a live agent, then score
- [CALLER-LOAD.md](CALLER-LOAD.md) - replay a bounded caller program under load
- [LOAD-AND-RECOVERY.md](LOAD-AND-RECOVERY.md) - schedule calls under load, keep per-call evidence
- [COUNTEREXAMPLES.md](COUNTEREXAMPLES.md) - reduce a scripted failure to a minimal repro
- [PIPER-CALLER-TTS.md](PIPER-CALLER-TTS.md) - local Piper TTS adapter for caller speech
- [scenarios/browser-vs-pstn.md](scenarios/browser-vs-pstn.md) - score the same moment through telephony degradation
- [scenarios/load-and-recovery.md](scenarios/load-and-recovery.md) - behaviour under concurrent load, with receipts

### Fix and gate, the long way

- [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md) - turn one bad call into a CI gate, step by step
- [STARTER.md](STARTER.md) - `hotato lab init starter` scaffolds a CI gate and config
- [PYTEST.md](PYTEST.md) - the pytest fixture and opt-in session gate
- [FIX-LOOP.md](FIX-LOOP.md) - the closed loop: find, fix, prove it is fixed
- [FIX-PLANS.md](FIX-PLANS.md) - the guarded fix ladder: diagnose, inspect, plan, apply
- [FIX-TRIAL.md](FIX-TRIAL.md) - before/after fix proof, fail-closed and clone-only
- [APPLY.md](APPLY.md) - guarded, clone-only staged apply of a fix patch
- [RECAPTURE.md](RECAPTURE.md) - prove the current agent, not the frozen recording
- [RELEASE-COMPARE.md](RELEASE-COMPARE.md) - diff two releases per dimension
- [CARDS.md](CARDS.md) - render one measured moment as a PR-native SVG card
- [scenarios/false-interruption-replay.md](scenarios/false-interruption-replay.md) - a false-stop becomes a contract replayed in CI

### Pipe your stack in

- [INGEST.md](INGEST.md) - a passive webhook on-ramp scanning completed calls
- [SET-AND-FORGET.md](SET-AND-FORGET.md) - a passive scheduled sweep for regression monitoring
- [TRANSPORT-RUNTIME.md](TRANSPORT-RUNTIME.md) - lifecycle, delivered media, and assertion facts across transports
- [CALLER-SIDECAR-PROTOCOL.md](CALLER-SIDECAR-PROTOCOL.md) - the caller/transport sidecar WebSocket protocol
- [LIVEKIT-CALLER-SESSION.md](LIVEKIT-CALLER-SESSION.md) - direct LiveKit room transport for the caller engine

### Fleet, reports, and benchmarks

- [GUARDIAN-FLEET.md](GUARDIAN-FLEET.md) - a control plane running the evidence workflow continuously
- [REPORTS.md](REPORTS.md) - reporting surfaces: doctor, report, team, export
- [BENCHMARK.md](BENCHMARK.md) - the measurement-error harness over labelled recordings
- [BENCH-SPEC.md](BENCH-SPEC.md) - frozen batteries, scoring protocol, verify
- [BENCHMARK-STACKS.md](BENCHMARK-STACKS.md) - run one battery through your configured stacks

## Contributing

- [SUBMITTING.md](SUBMITTING.md) - the full path from a call to a merged corpus entry
- [CORPUS-GOVERNANCE.md](CORPUS-GOVERNANCE.md) - consent, PII, and publishing rules for contributed calls
- [RFC-ROLEPLAY-FIXTURES.md](RFC-ROLEPLAY-FIXTURES.md) - a share-safe role-play fixture format
- [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) - the maintainer gates to clear before a release

See also the repository [`CONTRIBUTING.md`](../CONTRIBUTING.md), [`SECURITY.md`](../SECURITY.md), and [`CHANGELOG.md`](../CHANGELOG.md).
