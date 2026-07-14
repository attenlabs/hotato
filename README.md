<div align="center">

<img src=".github/assets/hotato-banner.svg" alt="hotato" width="440" style="max-width:100%;height:auto;">

<h1>hotato</h1>

</div>

<p align="center"><b>Open-source, self-hosted conversation QA for voice agents.</b></p>

<p align="center">
  Score every call across five dimensions kept apart (outcome, policy, conversation, speech, reliability), with the evidence behind each result. Deterministic checks and the model-judged rubric stay in separate columns.<br>
  Runs offline &middot; MIT &middot; zero dependencies.
</p>

<p align="center">
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/hotato.svg"></a>
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI monthly downloads" src="https://img.shields.io/pypi/dm/hotato.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.9 to 3.13" src="https://img.shields.io/badge/python-3.9%20to%203.13-blue.svg">
  <img alt="offline: yes" src="https://img.shields.io/badge/offline-yes-blue.svg">
  <img alt="runtime deps: zero" src="https://img.shields.io/badge/runtime%20deps-zero-blue.svg">
  <a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg"></a>
</p>

Your voice agent passes every text assertion and still loses the call: it talks over the caller, skips a required disclosure, confirms a refund that never posted. hotato scores the conversation and the outcome from the two-channel audio, hands you the timing evidence behind every flag, and turns a caught bug into a CI contract that re-checks it on every push.

## Quickstart: one command, on your machine

```bash
uvx hotato start --demo
```

That runs hotato with [uv](https://docs.astral.sh/uv/), no install step, on any machine. To keep it in a project, use pipx or a virtualenv:

```bash
pipx install hotato && hotato start --demo
# or: python -m venv .venv && . .venv/bin/activate && pip install hotato && hotato start --demo
```

Offline, this sweeps two bundled calls a default agent failed, writes the candidate dashboard, and turns one missed-interruption moment into a demo contract it verifies on the spot:

```
[start] demo: swept 2 bundled calls, 5 candidate moments;
  wrote hotato-sweep.json, hotato-sweep.html,
  hotato-no-single-threshold.svg,
  contracts/demo-missed-interruption.hotato/contract.json
hotato start: swept the 2 bundled demo calls offline.
  sweep dashboard: hotato-sweep.html
  demo contract:   contracts/demo-missed-interruption.hotato
  verified contract: FAIL as expected -- the demo call
    missed the interruption
  [ ... then the exact next commands: promote a candidate,
    gate it in CI, re-check it ... ]
```

`hotato-sweep.html` ranks candidate moments by how far the timing missed, each with a hear-the-bug playhead. Screenshot the top one into a PR.

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/sweep-dashboard.png" alt="hotato candidate dashboard: 5 moments ranked by salience, each with a caller/agent timeline, a hear-the-bug playhead, and promote-as-yield / promote-as-hold / ignore buttons." width="760" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub><code>hotato-sweep.html</code> &middot; candidates ranked by salience, each playhead synced to the audio. You label them into your verdict.</sub></p>

Point it at your own recording to walk the same loop:

```bash
# trust -> scan -> review -> label -> contract
hotato start --stereo my-call.wav
```

Every command takes a two-channel recording (caller on one channel, agent on the other). A mono file or a bad export is marked NOT SCORABLE, so every verdict rests on inputs that carry the timing evidence.

## Score a call: five dimensions, kept apart

<p align="center">
  <a href="https://hotato.dev"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato scorecard: one call graded across outcome, policy, conversation, speech, and reliability, deterministic checks kept separate from the model-judged rubric." width="760" style="max-width:100%;height:auto;"></a>
</p>

`hotato test run` grades one call against a conversation-test file, each dimension in its own count:

- **Outcome** &middot; did the job get done, graded on tool-call and state evidence, not the transcript's say-so.
- **Policy** &middot; required disclosures, PII handling, and the compliance phrases your team owns.
- **Conversation** &middot; the deterministic turn-taking core: did the agent yield when the caller took the floor, and how fast.
- **Speech** &middot; response latency and the timing around each turn.
- **Reliability** &middot; pass@1 / pass@k / pass^k across repeated runs with a Wilson interval, so a flaky check reads as flaky.

Deterministic and model-judged results never merge. The deterministic checks (phrase, PII, policy, tool-call, sequence, latency, outcome, all regex, checksum, span-lookup) set the gate; a rubric verdict is `deterministic: false`, advisory, and kept in its own count. Every dimension holds its own line, including under `--format json`, and the scored-output schemas reject an `overall_score` key outright.

```bash
# a starter you edit for your own call
hotato scenario init refund-check --out conversation-test.yaml
hotato test run conversation-test.yaml --agent support-bot
```

```
success: FAIL
  (required: all_deterministic_assertions_pass, no_rubric_failure)
per-dimension (grouped view; never blended):
  outcome       0 pass / 0 fail / 1 inconclusive
  policy        0 pass / 0 fail / 1 inconclusive
  conversation  0 pass / 0 fail / 1 inconclusive
  speech        0 pass / 0 fail / 1 inconclusive
  reliability   0 pass / 0 fail / 0 inconclusive
```

Supply the call as `--transcript`, `--trace`, `--state`, and/or `--audio`; each check turns pass or fail, and a check with no evidence stays INCONCLUSIVE. Walkthrough: [`docs/CONVERSATION-TEST.md`](docs/CONVERSATION-TEST.md).

The scored HTML report is the receipt: a per-event verdict, time-to-yield and talk-over histograms, failure clusters by fix class, and a two-channel timeline with the measured numbers, reproducible from the same audio and config.

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/report-scored.png" alt="Scored hotato report: 0 of 1 events pass with a REGRESSION verdict, an analytics panel (time to yield, talk-over histogram, failure clusters by fix class), and a per-event caller/agent timeline with the measured metrics." width="760" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub>One recording, the pinned scorer, a FAIL against the labeled yield expectation. Share it in a PR with <code>hotato card hotato-sweep.json#1 --out finding.svg</code>.</sub></p>

## The loop: catch, confirm, gate, prove

Surface a moment, confirm what it should have done, pin it to CI, then re-check today's agent.

**1. Catch.** `sweep` ranks the talk-over and false-stop moments across your recent calls by how far the timing missed, each a reproducible measurement from the open scorer:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/talk-over-card.svg" alt="Level 1 candidate card: 0.32s of overlap while the agent was talking, at t=2s." width="480" style="max-width:100%;height:auto;">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/false-stop-card.svg" alt="Level 1 candidate card: 0.46s of silence after the agent stopped with no caller nearby, at t=1.28s." width="480" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub><b>Level 1 -- candidate.</b> Measured, not judged (0.32s of overlap; 0.46s of trailing silence): a timing moment worth review.</sub></p>

**2. Confirm.** You label the expected behavior: `yield` (stop for the caller) or `hold` (keep talking through a backchannel). Intent stays yours. Because a threshold that stops missing interruptions starts false-stopping on backchannels, when both fail in one run `diagnose` surfaces the tradeoff rather than naming one threshold:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/no-single-threshold-card.svg" alt="One sensitivity dial trades a missed interruption against a false stop on a backchannel; hotato surfaces the tradeoff, fix class engagement-control." width="760" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub><b>Level 2 -- human-labeled failure.</b> A reviewer confirms a broken yield-or-hold expectation; the fix lives in the engagement-control class.</sub></p>

**3. Gate.** `fixture promote` saves the labeled call as a permanent regression test, and hotato speaks CI natively:

- A deterministic fail exits non-zero, a pass exits zero, so a red build is a caught regression.
- `hotato contract verify contracts/ --junit contracts-junit.xml` writes JUnit XML your runner already renders.
- `--format json` carries an `exit_code` field, so an agent reads the verdict without parsing prose.
- The model-judged rubric is advisory by default and blocks a build only when you pass `--gate`.

Drop-in GitHub Action and pytest plugin: [`docs/CI.md`](docs/CI.md) &middot; [`docs/PYTEST.md`](docs/PYTEST.md). One bad call to a CI gate, step by step: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md) &middot; [`examples/bad-call-to-ci/`](examples/bad-call-to-ci/README.md).

**4. Prove.** The frozen recording catches evidence, threshold, or scorer drift. To check that the CURRENT agent still behaves, recapture the same scenario and score a NEW contract under the same policy:

```bash
# place the same call against today's agent, capture
# dual-channel, then:
hotato contract create --stereo fresh-call.wav --onset 41.90 --expect yield \
    --id refund-cutoff-001-recapture --out contracts
hotato contract verify contracts/refund-cutoff-001-recapture.hotato
```

<p align="center"><sub><b>Level 4 -- fresh-recapture comparison.</b> A newly captured call meets the same labeled policy and every submitted paired guard held. Walkthrough: <a href="docs/RECAPTURE.md"><code>docs/RECAPTURE.md</code></a>.</sub></p>

### Five levels of evidence, each on its own lane

Every card, report, and CLI result names its evidence level; the public tier is the weakest one its inputs support.

| Level | Name | What it means |
| --- | --- | --- |
| 1 | Candidate | A candidate timing moment worth human review. |
| 2 | Human-labeled failure | A reviewer confirmed this recording broke an explicit yield-or-hold expectation. |
| 3 | Stored-evidence check | The historical audio still produces the expected result under the pinned policy and scorer. |
| 4 | Fresh-recapture comparison | A newly captured call passed the same contract, and no submitted paired guard regressed. |
| 5 | External proof | An independent team confirms a caught regression or a fresh recapture. |

A before/after experiment (`hotato fix trial`, and the fleet loop) re-derives every verdict from the on-disk audio under one pinned trial manifest.

## Scale one call into a release gate

- `hotato suite run suite.yaml --agent support-bot` -- a whole `suite.v1` offline through the deterministic scripted-caller simulator, into the local registry.
- `hotato simulate --matrix scenario.yaml --out ./conv` -- expand a `scenario.v1` matrix into hundreds of seeded, byte-identical `origin=simulated` runs.
- `hotato rubric run --rubrics rubrics.yaml --transcript call.json` -- the model-judged lane on a pinned local model; zero egress, advisory unless `--gate`. [`docs/RUBRIC.md`](docs/RUBRIC.md).
- `hotato release compare BASELINE CANDIDATE` -- diff two recorded releases per dimension and scenario, digest-exact, surfacing new and fixed failures.
- `hotato serve` -- a read-only, token-authenticated web app over the local registry (release readiness, scenario matrix, conversation inspector, failure clusters, production health) on `127.0.0.1`. [`docs/WORKSPACE.md`](docs/WORKSPACE.md).

The bundled reference agent shows the shape at scale: 25 jobs x 5 caller behaviours x 3 audio environments = 375 offline simulated runs, scored and byte-reproducible on replay. Run it with `make reference` ([`examples/reference-agent/`](examples/reference-agent/README.md), [`docs/SUITE-RUN.md`](docs/SUITE-RUN.md)); the measurement-error harness is [`docs/BENCHMARK.md`](docs/BENCHMARK.md).

## Connect a production stack

Point hotato at your live calls: connect once, then sweep on a schedule.

```bash
# credentials stored 0600, local only
hotato connect vapi
# cron, CI, wherever
hotato sweep --stack vapi --since 7d --out hotato-sweep.html
```

Your audio stays on your machine unless you pull it from your stack. Full guide: [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) &middot; [`examples/set-and-forget/`](examples/set-and-forget/README.md).

`sweep` and `hotato fleet run` post a one-line JSON summary to a webhook when they finish, off by default, opt in with `--notify` (repeatable):

```bash
hotato sweep --stack vapi --since 7d \
    --notify https://hooks.slack.com/services/...
```

The payload is metadata-only: counts, top candidate moments (id, kind, timing numbers), artifact paths, and a Slack-ready `text` field; a down webhook leaves the run intact, a delivery failure is one stderr warning. Egress details: [`docs/EGRESS.md`](docs/EGRESS.md).

## Fleet: private, self-hosted

`hotato fleet` runs the loop across every agent from one local workspace: ingest calls, surface candidates, label them, and run a before/after experiment that recomputes both sides from audio under a pinned manifest. It recommends a change and leaves the deploy to you. Local mode is stdlib-only (SQLite plus a content-addressed store).

```bash
hotato fleet init -w acme
hotato fleet agent add -w acme --name support-bot \
    --stack vapi --assistant-id asst_123
hotato fleet ingest -w acme --agent support-bot call.wav
hotato fleet discover -w acme --agent support-bot call.wav
hotato fleet review -w acme
```

With history, `hotato fleet trend -w acme` writes one self-contained HTML page: per-agent talk-over and time-to-yield trends (p50/p95 per day), candidate moments over time, and experiment outcomes (improved/inconclusive/refused). A short series reports "not enough history to trend" over an interpolated line. Full guide: [`docs/GUARDIAN-FLEET.md`](docs/GUARDIAN-FLEET.md).

## Self-host in your own cloud or VPC

The whole team workspace ships as a container. One command stands up the read-only, token-authenticated `hotato serve` on host loopback over a private volume, keeping all traffic on your own infrastructure:

```bash
# workspace on 127.0.0.1:8321
docker compose up -d
# optional: seed example data
docker compose run --rm hotato-init
# optional: a local Ollama model judge
docker compose --profile judge up -d
```

Your own calls, the local judge, air-gap, backup, and the zero-migration promise (self-host and cloud share one set of schemas): [`docs/SELF-HOST.md`](docs/SELF-HOST.md). Confirm the offline posture on your own machine with [`deploy/verify-zero-egress.sh`](deploy/verify-zero-egress.sh).

## Built for coding agents

hotato is a first-class tool for an LLM or an agent to drive: machine JSON on every command, meaningful exit codes, a described capability manifest, [`llms.txt`](llms.txt), JSON-LD, and an MCP server.

```bash
# the voice_eval_run scorer + eleven fleet tools
uvx --from "hotato[mcp]" hotato-mcp
```

Configs and the tool contract: [`docs/MCP.md`](docs/MCP.md) &middot; [`AGENTS.md`](AGENTS.md) &middot; [`llms-full.txt`](llms-full.txt).

## Choose your path

| You want to | Run this |
| --- | --- |
| Try the full loop, no credentials | `hotato start --demo` |
| Sweep the bundled demo calls | `hotato sweep --demo` |
| Sweep recent calls from your stack | `hotato connect vapi` then `hotato sweep --stack vapi --since 7d` |
| Add hotato to an existing repo, CI gate included | `hotato init starter --stack vapi --out .` ([`docs/STARTER.md`](docs/STARTER.md)) |
| Turn a confirmed failure into a portable contract | `hotato contract create --from-candidate hotato-sweep.json#1 --expect yield --id refund-cutoff-001 --out contracts` ([`docs/CONTRACTS.md`](docs/CONTRACTS.md)) |
| Verify contracts in CI | `hotato contract verify contracts/ --junit contracts-junit.xml` |
| Attach observability traces to a contract | `hotato trace attach contracts/refund-cutoff-001.hotato --trace voice_trace.jsonl` ([`docs/TRACE.md`](docs/TRACE.md)) |
| Test a candidate fix, before/after, fail-closed | `hotato fix trial patch.json --name staging-x --before before/ --after after/` ([`docs/FIX-TRIAL.md`](docs/FIX-TRIAL.md)) |
| Reduce a scripted deterministic failure to a verified repro | `hotato counterexample compile --scenario case.json --test test.json --target assertion-id --out case.hotato-repro` ([`docs/COUNTEREXAMPLES.md`](docs/COUNTEREXAMPLES.md)) |
| Share a finding in a PR or slide | `hotato card hotato-sweep.json#1 --out finding.svg` |
| Drive it from a coding agent | `uvx --from "hotato[mcp]" hotato-mcp` ([`docs/MCP.md`](docs/MCP.md)) |

`contract verify` and a promoted fixture in CI are two different guarantees, depending on which recording goes in:

**On the frozen recording (every push)**
- Proves: The evidence, policy, and scorer are intact
- Does not prove: That the deployed agent has not changed

**On a fresh recapture** (by hand, see [`docs/RECAPTURE.md`](docs/RECAPTURE.md))
- Proves: The CURRENT agent's behavior still matches the label
- Does not prove: --

A contract bundle contains call audio. Do not commit a raw customer contract to a public repository; use sanitized fixtures for anything public. See [`docs/CONTRACTS.md`](docs/CONTRACTS.md).

## Install

Run any command zero-install with [`uvx`](https://docs.astral.sh/uv/), or add hotato to a project with pipx or pip in a virtualenv:

```bash
# zero-install, any command:
uvx hotato start --demo
# keep it in a project:
pipx install hotato
# extras (Silero VAD cross-check / ASR transcript / LiveKit / Pipecat capture):
pipx install 'hotato[neural]'
pipx install 'hotato[transcribe]'
pipx install 'hotato[livekit]'
pipx install 'hotato[pipecat]'
# run an extra zero-install:
uvx --from 'hotato[neural]' hotato start --demo
```

## Contribute a labeled call

The highest-value PR is one labeled dual-channel call: the corpus is the part of hotato that compounds, and every labeled moment sharpens every scorer. Add a clip: [`docs/SUBMITTING.md`](docs/SUBMITTING.md) &middot; corpus and schema in [`corpus/`](corpus/README.md), recorded battery in [`corpus/vapi-defaults/README.md`](corpus/vapi-defaults/README.md). Contributor guide: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Where hotato fits

- **Conversation QA that shows its work**, sitting next to your runtime voice layers: [`docs/COMPARE.md`](docs/COMPARE.md).
- **Audio-timing scoring.** The opt-in `--transcribe` flag attaches an ASR transcript as context beside the verdict; the timing score stays grounded in the audio: [`docs/TRANSCRIBE.md`](docs/TRANSCRIBE.md).
- **Offline, out-of-band, anonymous.** It reads recordings after the call over two channels, so your live audio path and running agent stay in your hands and the channels stay anonymous.

## Docs

- **Set-and-forget monitoring**: [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) &middot; [`examples/set-and-forget/`](examples/set-and-forget/README.md)
- **Bad call to CI, step by step**: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md) &middot; [`examples/bad-call-to-ci/`](examples/bad-call-to-ci/README.md)
- **What it measures** (three timing signals): [`METHODOLOGY.md`](METHODOLOGY.md) &middot; [`docs/API.md`](docs/API.md)
- **The fix ladder** (failure &rarr; fix class + setting direction): [`docs/FIX-PLANS.md`](docs/FIX-PLANS.md)
- **Rule out non-turn-taking bugs first** (STT, buffering, verbosity, refusals, language): [`docs/WHY.md`](docs/WHY.md)
- **Pull a call from your stack** (Vapi, Twilio, Retell, LiveKit, Pipecat): [`adapters/README.md`](adapters/README.md) &middot; [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md)
- **CI gates**: [`docs/CI.md`](docs/CI.md) &middot; [`docs/PYTEST.md`](docs/PYTEST.md)
- **Recorded-call battery** (12 scripted calls on provider defaults): [`corpus/vapi-defaults/README.md`](corpus/vapi-defaults/README.md)
- **Failure contracts and traces**: [`docs/CONTRACTS.md`](docs/CONTRACTS.md) &middot; [`docs/TRACE.md`](docs/TRACE.md) &middot; [`docs/OTEL.md`](docs/OTEL.md)
- **Deterministic assertions** (phrase, PII, policy, tool-call, outcome): [`docs/ASSERTIONS.md`](docs/ASSERTIONS.md)
- **Checking the CURRENT agent, not just the frozen recording**: [`docs/RECAPTURE.md`](docs/RECAPTURE.md)
- **Egress** (per-command network table): [`docs/EGRESS.md`](docs/EGRESS.md)
- **Explain a failure, trial a fix**: [`docs/EXPLAIN.md`](docs/EXPLAIN.md) &middot; [`docs/FIX-TRIAL.md`](docs/FIX-TRIAL.md) &middot; [`docs/APPLY.md`](docs/APPLY.md) &middot; [`docs/FIX-LOOP.md`](docs/FIX-LOOP.md)
- **Evidence**: [`docs/VALIDATION.md`](docs/VALIDATION.md) &middot; [`docs/TRUST-MATRIX.md`](docs/TRUST-MATRIX.md) &middot; [`docs/GALLERY.md`](docs/GALLERY.md) &middot; [`docs/EVIDENCE-PACK.md`](docs/EVIDENCE-PACK.md) &middot; [`docs/COMPARE.md`](docs/COMPARE.md)
- **For coding agents**: [`AGENTS.md`](AGENTS.md) &middot; [`llms.txt`](llms.txt) &middot; [`llms-full.txt`](llms-full.txt) &middot; [`docs/MCP.md`](docs/MCP.md) &middot; [`SECURITY.md`](SECURITY.md)
- **Contributing** (a labeled call fixture): [`docs/SUBMITTING.md`](docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
