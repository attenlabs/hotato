<div align="center">

<img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/assets/hotato-banner.svg" alt="hotato" width="440" style="max-width:100%;height:auto;">

<h1>hotato</h1>

</div>

<p align="center"><b>Open-source, self-hosted conversation QA for voice agents.</b></p>

<p align="center">Runs offline &middot; MIT &middot; zero dependencies.</p>

<p align="center">
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/hotato.svg"></a>
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI monthly downloads" src="https://img.shields.io/pypi/dm/hotato.svg"></a>
  <a href="https://github.com/attenlabs/hotato/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.9 to 3.13" src="https://img.shields.io/badge/python-3.9%20to%203.13-blue.svg">
  <img alt="offline: yes" src="https://img.shields.io/badge/offline-yes-blue.svg">
  <img alt="runtime deps: zero" src="https://img.shields.io/badge/runtime%20deps-zero-blue.svg">
  <a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg"></a>
</p>

Your voice agent passes every text assertion and still loses the call. It talks over the caller. It skips a required disclosure. It confirms a refund that never posted. hotato scores the call from the two-channel audio, shows the timing evidence behind every flag, and turns each caught bug into a CI contract that re-checks it on every push.

## See a real bug in one command

```bash
uvx hotato start --demo
```

Runs with [uv](https://docs.astral.sh/uv/), no install. Or keep it in a project with pipx or pip:

```bash
pipx install hotato && hotato start --demo
# or: python -m venv .venv && . .venv/bin/activate && pip install hotato && hotato start --demo
```

Offline, it sweeps two failing demo calls and verifies one missed interruption on the spot:

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

`hotato-sweep.html` ranks the moments by how far the timing missed, each with a hear-the-bug playhead to screenshot into a PR.

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/sweep-dashboard.png" alt="hotato candidate dashboard: 5 moments ranked by salience, each with a caller/agent timeline and a hear-the-bug playhead." width="760" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub><code>hotato-sweep.html</code> &middot; candidates ranked by salience. You label them into your verdict.</sub></p>

Run the same loop on your own recording:

```bash
# trust -> scan -> review -> label -> contract
hotato start --stereo my-call.wav
```

Every command takes a two-channel recording (caller on one channel, agent on the other). A mono or bad export is marked NOT SCORABLE, so every verdict rests on inputs that carry the timing evidence.

## Score a call: five dimensions, kept apart

<p align="center">
  <a href="https://hotato.dev"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato scorecard: one call graded across outcome, policy, conversation, speech, and reliability, deterministic checks kept apart from the model-judged rubric." width="760" style="max-width:100%;height:auto;"></a>
</p>

`hotato test run` grades one call against a conversation-test file, one count per dimension:

- **Outcome** &middot; did the job get done, on tool-call and state evidence, not the transcript.
- **Policy** &middot; required disclosures, PII handling, and your team's compliance phrases.
- **Conversation** &middot; the deterministic turn-taking core: did the agent yield when the caller took the floor, and how fast.
- **Speech** &middot; response latency and the timing around each turn.
- **Reliability** &middot; pass@1 / pass@k / pass^k over repeated runs with a Wilson interval, so a flaky check reads as flaky.

Deterministic and model-judged results stay in separate columns. The deterministic checks set the gate; a rubric verdict is `deterministic: false` and advisory. Every dimension keeps its own line, even under `--format json`, and the scored schemas reject an `overall_score` key.

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

Feed the call as `--transcript`, `--trace`, `--state`, and/or `--audio`. A check with no evidence stays INCONCLUSIVE. Walkthrough: [`docs/CONVERSATION-TEST.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONVERSATION-TEST.md).

The scored HTML report below is the receipt, reproducible from the same audio and config.

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/report-scored.png" alt="Scored hotato report: 0 of 1 events pass, a REGRESSION verdict, an analytics panel, and a per-event caller/agent timeline with the measured metrics." width="760" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub>The pinned scorer, a FAIL against the labeled yield expectation. Share it with <code>hotato card hotato-sweep.json#1 --out finding.svg</code>.</sub></p>

## The loop: catch, confirm, gate, prove

**1. Catch.** `sweep` ranks the talk-over and false-stop moments in your recent calls by how far the timing missed:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/talk-over-card.svg" alt="Level 1 candidate card: 0.32s of overlap while the agent was talking, at t=2s." width="480" style="max-width:100%;height:auto;">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/false-stop-card.svg" alt="Level 1 candidate card: 0.46s of silence after the agent stopped, at t=1.28s." width="480" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub><b>Level 1: candidate.</b> A timing moment worth review, measured not judged: 0.32s of overlap, 0.46s of trailing silence.</sub></p>

**2. Confirm.** You label the expected behavior: `yield` (stop for the caller) or `hold` (talk through a backchannel). Intent stays yours. One dial trades a missed interruption against a false stop, so when both fail in a run, `diagnose` surfaces the tradeoff instead of naming one threshold:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/no-single-threshold-card.svg" alt="One dial trades a missed interruption against a false stop on a backchannel; hotato surfaces the tradeoff, fix class engagement-control." width="760" style="max-width:100%;height:auto;">
</p>
<p align="center"><sub><b>Level 2: human-labeled failure.</b> A reviewer confirms a broken yield-or-hold expectation; the fix lives in the engagement-control class.</sub></p>

**3. Gate.** `fixture promote` saves the labeled call as a permanent regression test; hotato speaks CI natively:

- A deterministic fail exits non-zero, a pass exits zero: a red build is a caught regression.
- `hotato contract verify contracts/ --junit contracts-junit.xml` writes JUnit XML your runner already renders.
- `--format json` carries an `exit_code` field, so an agent reads the verdict without parsing prose.
- The model-judged rubric is advisory by default, blocking a build only with `--gate`.

Drop-in GitHub Action and pytest plugin: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md) &middot; [`docs/PYTEST.md`](https://github.com/attenlabs/hotato/blob/main/docs/PYTEST.md). One bad call to a CI gate, step by step: [`docs/BAD-CALL-TO-CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/BAD-CALL-TO-CI.md) &middot; [`examples/bad-call-to-ci/`](https://github.com/attenlabs/hotato/blob/main/examples/bad-call-to-ci/README.md).

**4. Prove.** The frozen recording catches evidence, threshold, or scorer drift. To re-check today's agent, recapture the scenario as a new contract under the same policy:

```bash
# place the same call against today's agent, capture
# dual-channel, then:
hotato contract create --stereo fresh-call.wav --onset 41.90 --expect yield \
    --id refund-cutoff-001-recapture --out contracts
hotato contract verify contracts/refund-cutoff-001-recapture.hotato
```

<p align="center"><sub><b>Level 4: fresh-recapture comparison.</b> A newly captured call meets the same labeled policy and every submitted paired guard held. Walkthrough: <a href="https://github.com/attenlabs/hotato/blob/main/docs/RECAPTURE.md"><code>docs/RECAPTURE.md</code></a>.</sub></p>

### Five levels of evidence, each on its own lane

Every card, report, and CLI result names its evidence level. The public tier is the weakest one its inputs support.

| Level | Name | What it means |
| --- | --- | --- |
| 1 | Candidate | A candidate timing moment worth human review. |
| 2 | Human-labeled failure | A reviewer confirmed this recording broke an explicit yield-or-hold expectation. |
| 3 | Stored-evidence check | The historical audio still produces the expected result under the pinned policy and scorer. |
| 4 | Fresh-recapture comparison | A newly captured call passed the same contract, and no submitted paired guard regressed. |
| 5 | External proof | An independent team confirms a caught regression or a fresh recapture. |

A before/after experiment (`hotato fix trial`, and the fleet loop) re-derives every verdict from the on-disk audio under one pinned manifest.

## Scale one call into a release gate

- `hotato suite run suite.yaml --agent support-bot` &middot; a `suite.v1` offline through the scripted-caller simulator.
- `hotato simulate --matrix scenario.yaml --out ./conv` &middot; expand a `scenario.v1` matrix into hundreds of seeded, byte-identical runs.
- `hotato rubric run --rubrics rubrics.yaml --transcript call.json` &middot; the model-judged lane on a pinned local model, advisory unless `--gate`. [`docs/RUBRIC.md`](https://github.com/attenlabs/hotato/blob/main/docs/RUBRIC.md).
- `hotato release compare BASELINE CANDIDATE` &middot; diff two recorded releases per dimension and scenario, digest-exact.
- `hotato serve` &middot; a read-only, token-authenticated web app over the local registry on `127.0.0.1`. [`docs/WORKSPACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/WORKSPACE.md).

The bundled reference agent runs 375 offline, byte-reproducible runs (25 jobs x 5 caller behaviours x 3 audio environments): `make reference` ([`examples/reference-agent/`](https://github.com/attenlabs/hotato/blob/main/examples/reference-agent/README.md), [`docs/SUITE-RUN.md`](https://github.com/attenlabs/hotato/blob/main/docs/SUITE-RUN.md)); measurement-error harness [`docs/BENCHMARK.md`](https://github.com/attenlabs/hotato/blob/main/docs/BENCHMARK.md).

## Point it at production, sweep on a schedule

Connect a live stack once, then sweep on cron or in CI:

```bash
# credentials stored 0600, local only
hotato connect vapi
# cron, CI, wherever
hotato sweep --stack vapi --since 7d --out hotato-sweep.html
```

Your audio stays on your machine. Full guide: [`docs/SET-AND-FORGET.md`](https://github.com/attenlabs/hotato/blob/main/docs/SET-AND-FORGET.md) &middot; [`examples/set-and-forget/`](https://github.com/attenlabs/hotato/blob/main/examples/set-and-forget/README.md).

Opt in to a metadata-only webhook summary with `--notify` (repeatable):

```bash
hotato sweep --stack vapi --since 7d \
    --notify https://hooks.slack.com/services/...
```

Counts, top candidate moments, artifact paths, and a Slack-ready `text` field. Egress: [`docs/EGRESS.md`](https://github.com/attenlabs/hotato/blob/main/docs/EGRESS.md).

## Run every agent's loop from one private workspace

One local workspace across every agent: ingest, label, and run a before/after experiment that recomputes both sides from audio, then recommends a change and leaves the deploy to you.

```bash
hotato fleet init -w acme
hotato fleet agent add -w acme --name support-bot \
    --stack vapi --assistant-id asst_123
hotato fleet ingest -w acme --agent support-bot call.wav
hotato fleet discover -w acme --agent support-bot call.wav
hotato fleet review -w acme
```

`hotato fleet trend -w acme` writes a self-contained per-agent trend page. Full guide: [`docs/GUARDIAN-FLEET.md`](https://github.com/attenlabs/hotato/blob/main/docs/GUARDIAN-FLEET.md).

## Self-host in your own cloud or VPC

The team workspace ships as a container: one command stands up the read-only, token-authenticated `hotato serve` on host loopback.

```bash
# workspace on 127.0.0.1:8321
docker compose up -d
# optional: seed example data
docker compose run --rm hotato-init
# optional: a local Ollama model judge
docker compose --profile judge up -d
```

Air-gap, backup, a local judge, and one set of schemas for self-host and cloud: [`docs/SELF-HOST.md`](https://github.com/attenlabs/hotato/blob/main/docs/SELF-HOST.md). Verify the offline posture yourself with [`deploy/verify-zero-egress.sh`](https://raw.githubusercontent.com/attenlabs/hotato/main/deploy/verify-zero-egress.sh).

## Built for coding agents

hotato is built for agents to drive: machine JSON on every command, meaningful exit codes, a capability manifest, [`llms.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms.txt), JSON-LD, and an MCP server.

```bash
# the voice_eval_run scorer + eleven fleet tools
uvx --from "hotato[mcp]" hotato-mcp
```

Configs and the tool contract: [`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md) &middot; [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) &middot; [`llms-full.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms-full.txt).

## Choose your path

| You want to | Run this |
| --- | --- |
| Try the full loop, no credentials | `hotato start --demo` |
| Sweep the bundled demo calls | `hotato sweep --demo` |
| Sweep recent calls from your stack | `hotato connect vapi` then `hotato sweep --stack vapi --since 7d` |
| Add hotato to an existing repo, CI gate included | `hotato init starter --stack vapi --out .` ([`docs/STARTER.md`](https://github.com/attenlabs/hotato/blob/main/docs/STARTER.md)) |
| Turn a confirmed failure into a portable contract | `hotato contract create --from-candidate hotato-sweep.json#1 --expect yield --id refund-cutoff-001 --out contracts` ([`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md)) |
| Verify contracts in CI | `hotato contract verify contracts/ --junit contracts-junit.xml` |
| Attach observability traces to a contract | `hotato trace attach contracts/refund-cutoff-001.hotato --trace voice_trace.jsonl` ([`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md)) |
| Test a candidate fix, before/after, fail-closed | `hotato fix trial patch.json --name staging-x --before before/ --after after/` ([`docs/FIX-TRIAL.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-TRIAL.md)) |
| Reduce a scripted deterministic failure to a verified repro | `hotato counterexample compile --scenario case.json --test test.json --target assertion-id --out case.hotato-repro` ([`docs/COUNTEREXAMPLES.md`](https://github.com/attenlabs/hotato/blob/main/docs/COUNTEREXAMPLES.md)) |
| Share a finding in a PR or slide | `hotato card hotato-sweep.json#1 --out finding.svg` |
| Drive it from a coding agent | `uvx --from "hotato[mcp]" hotato-mcp` ([`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md)) |

`contract verify` and a promoted fixture are two guarantees, set by which recording goes in:

**On the frozen recording (every push)**
- Proves: the evidence, policy, and scorer are intact.
- Does not prove: that the deployed agent has not changed.

**On a fresh recapture** (by hand, see [`docs/RECAPTURE.md`](https://github.com/attenlabs/hotato/blob/main/docs/RECAPTURE.md))
- Proves: today's agent behavior still matches the label.

A contract bundle contains call audio, so keep raw customer contracts out of public repos; use sanitized fixtures. See [`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md).

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

The highest-value PR is one labeled dual-channel call: the corpus compounds, every labeled moment sharpening every scorer. Add a clip: [`docs/SUBMITTING.md`](https://github.com/attenlabs/hotato/blob/main/docs/SUBMITTING.md) &middot; corpus and schema in [`corpus/`](https://github.com/attenlabs/hotato/blob/main/corpus/README.md), recorded battery in [`corpus/vapi-defaults/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/vapi-defaults/README.md). Contributor guide: [`CONTRIBUTING.md`](https://github.com/attenlabs/hotato/blob/main/CONTRIBUTING.md).

## Where hotato fits

- **Conversation QA that shows its work**, sitting next to your runtime voice layers: [`docs/COMPARE.md`](https://github.com/attenlabs/hotato/blob/main/docs/COMPARE.md).
- **Audio-timing scoring.** The opt-in `--transcribe` flag adds an ASR transcript beside the verdict, the score still grounded in the audio: [`docs/TRANSCRIBE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRANSCRIBE.md).
- **Offline, out-of-band, anonymous.** It reads recordings after the call over two channels, so your live audio path and running agent stay in your hands.

## Docs

- **Set-and-forget monitoring**: [`docs/SET-AND-FORGET.md`](https://github.com/attenlabs/hotato/blob/main/docs/SET-AND-FORGET.md) &middot; [`examples/set-and-forget/`](https://github.com/attenlabs/hotato/blob/main/examples/set-and-forget/README.md)
- **Bad call to CI, step by step**: [`docs/BAD-CALL-TO-CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/BAD-CALL-TO-CI.md) &middot; [`examples/bad-call-to-ci/`](https://github.com/attenlabs/hotato/blob/main/examples/bad-call-to-ci/README.md)
- **What it measures** (three timing signals): [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md) &middot; [`docs/API.md`](https://github.com/attenlabs/hotato/blob/main/docs/API.md)
- **The fix ladder** (failure &rarr; fix class): [`docs/FIX-PLANS.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-PLANS.md)
- **Rule out non-turn-taking bugs first** (STT, buffering, verbosity, refusals, language): [`docs/WHY.md`](https://github.com/attenlabs/hotato/blob/main/docs/WHY.md)
- **Pull a call from your stack** (Vapi, Twilio, Retell, LiveKit, Pipecat): [`adapters/README.md`](https://github.com/attenlabs/hotato/blob/main/adapters/README.md) &middot; [`docs/ADAPTER-STATUS.md`](https://github.com/attenlabs/hotato/blob/main/docs/ADAPTER-STATUS.md)
- **CI gates**: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md) &middot; [`docs/PYTEST.md`](https://github.com/attenlabs/hotato/blob/main/docs/PYTEST.md)
- **Recorded-call battery** (12 scripted calls): [`corpus/vapi-defaults/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/vapi-defaults/README.md)
- **Failure contracts and traces**: [`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md) &middot; [`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md) &middot; [`docs/OTEL.md`](https://github.com/attenlabs/hotato/blob/main/docs/OTEL.md)
- **Deterministic assertions** (phrase, PII, policy, tool-call, outcome): [`docs/ASSERTIONS.md`](https://github.com/attenlabs/hotato/blob/main/docs/ASSERTIONS.md)
- **Checking today's agent, not just the frozen recording**: [`docs/RECAPTURE.md`](https://github.com/attenlabs/hotato/blob/main/docs/RECAPTURE.md)
- **Egress** (per-command network table): [`docs/EGRESS.md`](https://github.com/attenlabs/hotato/blob/main/docs/EGRESS.md)
- **Explain a failure, trial a fix**: [`docs/EXPLAIN.md`](https://github.com/attenlabs/hotato/blob/main/docs/EXPLAIN.md) &middot; [`docs/FIX-TRIAL.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-TRIAL.md) &middot; [`docs/APPLY.md`](https://github.com/attenlabs/hotato/blob/main/docs/APPLY.md) &middot; [`docs/FIX-LOOP.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-LOOP.md)
- **Evidence**: [`docs/VALIDATION.md`](https://github.com/attenlabs/hotato/blob/main/docs/VALIDATION.md) &middot; [`docs/TRUST-MATRIX.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRUST-MATRIX.md) &middot; [`docs/GALLERY.md`](https://github.com/attenlabs/hotato/blob/main/docs/GALLERY.md) &middot; [`docs/EVIDENCE-PACK.md`](https://github.com/attenlabs/hotato/blob/main/docs/EVIDENCE-PACK.md) &middot; [`docs/COMPARE.md`](https://github.com/attenlabs/hotato/blob/main/docs/COMPARE.md)
- **For coding agents**: [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) &middot; [`llms.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms.txt) &middot; [`llms-full.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms-full.txt) &middot; [`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md) &middot; [`SECURITY.md`](https://github.com/attenlabs/hotato/blob/main/SECURITY.md)
- **Contributing** (a labeled call fixture): [`docs/SUBMITTING.md`](https://github.com/attenlabs/hotato/blob/main/docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
