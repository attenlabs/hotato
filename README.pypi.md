<div align="center">

<img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/assets/hotato-banner.svg" alt="hotato" width="440">

<h1>hotato</h1>

</div>

<p align="center"><b>Open-source, self-hosted conversation QA for voice agents.</b></p>

<p align="center">
  Score every call across five dimensions reported on their own (outcome, policy, conversation, speech, reliability), with the evidence behind every result. Deterministic checks and the model-judged rubric each score on their own lane.<br>
  Runs offline &middot; MIT &middot; zero dependencies.
</p>

<p align="center">
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/hotato.svg"></a>
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI monthly downloads" src="https://img.shields.io/pypi/dm/hotato.svg"></a>
  <a href="https://github.com/attenlabs/hotato/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.9 to 3.13" src="https://img.shields.io/badge/python-3.9%20to%203.13-blue.svg">
  <img alt="offline: yes" src="https://img.shields.io/badge/offline-yes-blue.svg">
  <img alt="runtime deps: zero" src="https://img.shields.io/badge/runtime%20deps-zero-blue.svg">
  <a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg"></a>
</p>

Your voice agent passes every text assertion and still loses the call. It talks over the caller, skips a required disclosure, or confirms a refund that never posted. hotato scores the conversation and the outcome, with the timing evidence behind every flag, then turns a caught bug into a CI contract so it stays caught.

## Quickstart: one command, on your machine

```bash
pip install hotato && hotato start --demo
```

Already have [uv](https://docs.astral.sh/uv/)? Zero-install, same command:

```bash
uvx hotato start --demo
```

One command sweeps two bundled calls a provider's default agent failed, writes the candidate dashboard, and turns one missed-interruption moment into a demo failure contract it verifies on the spot, all offline:


```
[start] demo: swept 2 bundled calls, 5 candidate moments; wrote hotato-sweep.json, hotato-sweep.html, hotato-no-single-threshold.svg, contracts/demo-missed-interruption.hotato/contract.json
hotato start: swept the 2 bundled demo calls offline.
  sweep dashboard: hotato-sweep.html
  demo contract:   contracts/demo-missed-interruption.hotato
  verified contract: FAIL as expected -- the demo call missed the interruption
  [ ... then the exact next commands: promote a candidate, gate it in CI, re-check it ... ]
```

Open `hotato-sweep.html`. The candidate moments come ranked by how far the timing missed, each with a hear-the-bug playhead. This is the artifact you screenshot into a PR:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/sweep-dashboard.png" alt="The hotato candidate dashboard: 5 candidate moments ranked by salience across 2 calls, each card showing the caller and agent timeline, an embedded hear-the-bug audio playhead, and promote-as-yield / promote-as-hold / ignore buttons." width="820">
</p>
<p align="center"><sub><code>hotato-sweep.html</code> &middot; candidate moments ranked by salience, each with a playhead that sweeps the timeline in sync with the embedded audio. Candidates you review and label into your verdict.</sub></p>

Point it at your own recording to walk the same loop end to end:

```bash
hotato start --stereo my-call.wav          # trust -> scan -> review -> label -> contract
```

Every command takes a two-channel recording (caller on one channel, agent on the other). A mono file or a bad export is marked NOT SCORABLE, so every verdict rests on inputs that carry the timing evidence.

## What hotato catches: five dimensions, reported on their own

<p align="center">
  <a href="https://hotato.dev"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="A hotato scorecard: one call graded across outcome, policy, conversation, speech, and reliability, deterministic checks kept separate from the model-judged rubric, each dimension scored on its own." width="840"></a>
</p>


A voice agent can pass every text assertion and still lose the call: the refund never fires, the recording disclosure gets skipped, the caller gets talked over. `hotato test run` grades one call against a conversation-test file across five dimensions, each kept apart in its own count:

- **Outcome** &middot; did the job get done, graded on tool-call and state evidence, not the transcript's say-so.
- **Policy** &middot; required disclosures, PII handling, and the compliance phrases your team owns.
- **Conversation** &middot; the deterministic turn-taking core: did the agent yield when the caller took the floor, and how fast.
- **Speech** &middot; response latency and the timing around each turn.
- **Reliability** &middot; pass@1 / pass@k / pass^k across repeated runs with a Wilson interval, so a flaky check reads as flaky.

Two lanes stay structurally separate. Deterministic checks (phrase, PII, policy, tool-call, sequence, latency, outcome, all pure regex, checksum, and span-lookup) live behind a wall from the model-judged rubric lane. A rubric verdict is `deterministic: false` and advisory, kept in its own count; the deterministic lane sets the gate. Each dimension keeps its own line in the output, including `--format json`, and the scored-output schemas reject an `overall_score` key outright.

```bash
hotato scenario init refund-check --out conversation-test.yaml   # a starter you edit for your own call
hotato test run conversation-test.yaml --agent support-bot
```

```
success: FAIL  (required: all_deterministic_assertions_pass, no_rubric_failure)
per-dimension (grouped view; never blended):
  outcome       0 pass / 0 fail / 1 inconclusive
  policy        0 pass / 0 fail / 1 inconclusive
  conversation  0 pass / 0 fail / 1 inconclusive
  speech        0 pass / 0 fail / 1 inconclusive
  reliability   0 pass / 0 fail / 0 inconclusive
```

Supply the call as `--transcript`, `--trace`, `--state`, and/or `--audio` and each check turns to pass or fail; a check whose evidence is absent stays INCONCLUSIVE until the evidence arrives. Full walkthrough: [`docs/CONVERSATION-TEST.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONVERSATION-TEST.md).

## The receipt: a per-dimension scorecard with the evidence attached

The scored HTML report is the thing that ends an argument in review: a verdict per event, a time-to-yield and talk-over histogram, failure clusters by fix class, and the per-event timeline with both channels and the measured numbers. The same audio and config reproduce every figure on the page.

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/report-scored.png" alt="The scored hotato HTML report: 0 of 1 events pass with a REGRESSION verdict, an analytics panel (time to yield, talk-over histogram, failure clusters by fix class), and the per-event timeline with caller and agent channels, the measured metrics, and the fix note." width="820">
</p>
<p align="center"><sub>One recording, the pinned scorer, a FAIL against the labeled yield expectation. Share it in a PR with <code>hotato card hotato-sweep.json#1 --out finding.svg</code>.</sub></p>

## The loop: catch, confirm, gate, prove

Surface a moment, confirm what it should have done, pin it to a CI contract, then re-check today's agent.

### 1. Catch: surface the candidate moments

`sweep` ranks the talk-over and false-stop moments across your recent calls by how far the timing missed. Two candidate types, straight from the open scorer, each a reproducible timing measurement:

<table align="center">
<tr>
<td width="412"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/talk-over-card.svg" alt="Level 1 candidate card: 0.32s of overlap while the agent was talking, at t=2s in the recording. Hotato reports the measured timing." width="400"></td>
<td width="412"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/false-stop-card.svg" alt="Level 1 candidate card: 0.46s of silence after the agent stopped with no caller nearby, at t=1.28s in the recording. Hotato reports the measured timing." width="400"></td>
</tr>
<tr>
<td colspan="2" align="center"><sub><b>Level 1 -- candidate.</b> Hotato reports what it measured (0.32s of overlap; 0.46s of trailing silence). Each is a candidate timing moment worth review.</sub></td>
</tr>
</table>

### 2. Confirm: you label yield or hold

You decide the expected behavior for a candidate: `yield` (stop for the caller) or `hold` (keep talking through a backchannel). That judgment stays with you: a threshold that stops missing interruptions starts false-stopping on backchannels, so when both fail in one run `diagnose` surfaces the tradeoff instead of naming one threshold:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/no-single-threshold-card.svg" alt="One sensitivity dial trades a missed interruption against a false stop on a backchannel, so hotato surfaces the tradeoff and leaves threshold tuning aside; fix class engagement-control." width="760">
</p>
<p align="center"><sub><b>Level 2 -- human-labeled failure.</b> A reviewer confirms the recording broke an explicit yield-or-hold expectation. When a missed interruption and a false stop collide, the fix lives in the engagement-control class.</sub></p>

### 3. Gate: pin it to a CI contract

`fixture promote` saves your labeled call as a permanent regression test. On every push, CI re-scores that recording under the pinned thresholds and scorer, and exits non-zero if the stored evidence stops matching your policy.

### 4. Prove: re-check the current agent

The frozen recording catches the evidence, thresholds, or scorer drifting. To check that the CURRENT agent still behaves, recapture the same scenario and score a NEW contract under the same policy:

```bash
# place the same call against today's agent, capture dual-channel, then:
hotato contract create --stereo fresh-call.wav --onset 41.90 --expect yield \
    --id refund-cutoff-001-recapture --out contracts
hotato contract verify contracts/refund-cutoff-001-recapture.hotato
```

<p align="center"><sub><b>Level 4 -- fresh-recapture comparison.</b> A newly captured call meets the same labeled policy and every submitted paired guard held. This is the claim only a fresh recapture can make. Walkthrough: <a href="https://github.com/attenlabs/hotato/blob/main/docs/RECAPTURE.md"><code>docs/RECAPTURE.md</code></a>.</sub></p>

### Five levels of evidence, each scored on its own lane

Every card, report, and CLI result names its evidence level, and the public tier is the weakest one its inputs support.

| Level | Name | What it means |
| --- | --- | --- |
| 1 | Candidate | A candidate timing moment worth human review. |
| 2 | Human-labeled failure | A reviewer confirmed this recording broke an explicit yield-or-hold expectation. |
| 3 | Stored-evidence check | The historical audio still produces the expected result under the pinned policy and scorer. |
| 4 | Fresh-recapture comparison | A newly captured call passed the same contract, and no submitted paired guard regressed. |
| 5 | External proof | An independent team confirmed a caught regression or a fresh recapture. Not yet published. |

A before/after experiment (`hotato fix trial`, and the fleet loop) re-derives every verdict from the on-disk audio under one pinned trial manifest. A proof stands on the original recordings, intact fixtures, and that manifest, so the number is re-scored from the audio on every run.

## Gate it in CI

hotato speaks CI natively.

- Every scoring command exits non-zero when a deterministic check fails and zero when they pass, so a red build is a caught regression.
- `hotato contract verify contracts/ --junit contracts-junit.xml` writes JUnit XML your runner already renders.
- `--format json` carries an `exit_code` field, so an agent or a workflow reads the verdict without parsing prose.
- The model-judged rubric is advisory by default and blocks a build only when you pass `--gate`.

Drop-in GitHub Action and a pytest plugin: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md) &middot; [`docs/PYTEST.md`](https://github.com/attenlabs/hotato/blob/main/docs/PYTEST.md). One bad call to a CI gate, step by step: [`docs/BAD-CALL-TO-CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/BAD-CALL-TO-CI.md) &middot; runnable [`examples/bad-call-to-ci/`](https://github.com/attenlabs/hotato/blob/main/examples/bad-call-to-ci/README.md).

## Scale one call into a release gate

- `hotato suite run suite.yaml --agent support-bot` -- run a whole `suite.v1`; scenario-driven tests execute offline through the deterministic scripted-caller simulator, and every run records into the local registry.
- `hotato simulate --matrix scenario.yaml --out ./conv` -- render a `scenario.v1` with a deterministic scripted caller into `origin=simulated` conversation artifacts; the variation matrix expands into hundreds of runs, seeded and byte-identical on replay.
- `hotato rubric run --rubrics rubrics.yaml --transcript call.json` -- the model-judged lane on a pinned local model; zero egress, advisory unless `--gate`. [`docs/RUBRIC.md`](https://github.com/attenlabs/hotato/blob/main/docs/RUBRIC.md).
- `hotato release compare BASELINE CANDIDATE` -- diff two recorded releases per dimension and per scenario, digest-exact, surfacing new failures and fixed-since.
- `hotato serve` -- a self-hosted, read-only web app over the local registry (release readiness, scenario matrix, conversation inspector, failure clusters, production health) on `127.0.0.1`, bearer-token authenticated. [`docs/WORKSPACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/WORKSPACE.md).

The bundled reference agent shows the shape at scale: 25 voice-agent jobs x 5 caller behaviours x 3 audio environments = 375 offline simulated runs, scored across the dimensions and byte-reproducible on replay. Run it with `make reference` in [`examples/reference-agent/`](https://github.com/attenlabs/hotato/blob/main/examples/reference-agent/README.md) ([`docs/SUITE-RUN.md`](https://github.com/attenlabs/hotato/blob/main/docs/SUITE-RUN.md)); the measurement-error harness is [`docs/BENCHMARK.md`](https://github.com/attenlabs/hotato/blob/main/docs/BENCHMARK.md).

## Connect a production stack

The demo runs on the bundled calls alone. To point Hotato at your live calls, connect once, then sweep on a schedule:

```bash
hotato connect vapi                                             # credentials stored 0600, local only
hotato sweep --stack vapi --since 7d --out hotato-sweep.html    # cron, CI, wherever
```

Run `sweep` on a timer and it becomes a scheduled batch scanner. Your audio stays on your machine unless you explicitly pull it from your stack. Full guide: [`docs/SET-AND-FORGET.md`](https://github.com/attenlabs/hotato/blob/main/docs/SET-AND-FORGET.md) &middot; runnable [`examples/set-and-forget/`](https://github.com/attenlabs/hotato/blob/main/examples/set-and-forget/README.md).

`sweep` and `hotato fleet run` can POST a one-line JSON summary to a webhook when they finish, off by default, opt in with `--notify` (repeatable for more than one URL):

```bash
hotato sweep --stack vapi --since 7d --notify https://hooks.slack.com/services/...
```

The payload stays metadata-only: counts, the top candidate moments (id, kind, timing numbers only), local artifact paths, and a `text` field a Slack incoming webhook renders directly. A down or slow webhook leaves the run intact: a delivery failure is one warning line on stderr. Egress details: [`docs/EGRESS.md`](https://github.com/attenlabs/hotato/blob/main/docs/EGRESS.md).

## Fleet: private, self-hosted

`hotato fleet` runs the loop across every agent from one local workspace: ingest calls, surface candidates, label them, and run a before/after experiment that recomputes both sides from audio under a pinned manifest. It recommends a change and leaves the deploy to you. Local mode is stdlib-only (SQLite plus a content-addressed store), runs entirely on your machine, and registers as many agents as you want.

```bash
hotato fleet init    -w acme
hotato fleet agent add -w acme --name support-bot --stack vapi --assistant-id asst_123
hotato fleet ingest  -w acme --agent support-bot call.wav
hotato fleet discover -w acme --agent support-bot call.wav
hotato fleet review   -w acme
```

Once a workspace has some history, `hotato fleet trend -w acme` reads the same local SQLite registry and writes one self-contained HTML page: per-agent talk-over and time-to-yield trend lines (p50/p95 per day), candidate moments discovered over time, and experiment outcomes (improved/inconclusive/refused). A day with no measurements gets no point, and a series with fewer than two days of history reports plainly as "not enough history to trend" instead of an interpolated line. Full guide: [`docs/GUARDIAN-FLEET.md`](https://github.com/attenlabs/hotato/blob/main/docs/GUARDIAN-FLEET.md).

## Self-host in your own cloud or VPC

The whole team workspace ships as a container. One command stands up the read-only, token-authenticated workspace (`hotato serve`) on host loopback over a private volume; the default stack keeps all traffic on your own infrastructure at run time.

```bash
docker compose up -d                    # workspace on 127.0.0.1:8321
docker compose run --rm hotato-init     # optional: seed example data
docker compose --profile judge up -d    # optional: a local Ollama model judge
```

Build, connect your own calls, the local judge, air-gap, backup, and the zero-migration promise (self-host and cloud share one set of schemas): [`docs/SELF-HOST.md`](https://github.com/attenlabs/hotato/blob/main/docs/SELF-HOST.md). Confirm the offline posture on your own machine with [`deploy/verify-zero-egress.sh`](https://raw.githubusercontent.com/attenlabs/hotato/main/deploy/verify-zero-egress.sh).

## Built for coding agents

Hotato is a first-class tool for an LLM or an agent to drive: machine JSON on every command, meaningful exit codes, a described capability manifest, [`llms.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms.txt), JSON-LD, and an MCP server.

```bash
uvx --from "hotato[mcp]" hotato-mcp      # the voice_eval_run scorer + eleven fleet tools
```

Configs and the tool contract: [`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md) &middot; [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) &middot; [`llms-full.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms-full.txt).

## Choose your path

| You want to | Run this |
| --- | --- |
| Try the full loop, no credentials | `hotato start --demo` |
| Sweep the bundled demo calls | `hotato sweep --demo` |
| Sweep recent calls from your stack | `hotato connect vapi` then `hotato sweep --stack vapi --since 7d` |
| Add Hotato to an existing repo, CI gate included | `hotato init starter --stack vapi --out .` ([`docs/STARTER.md`](https://github.com/attenlabs/hotato/blob/main/docs/STARTER.md)) |
| Turn a confirmed failure into a portable contract | `hotato contract create --from-candidate hotato-sweep.json#1 --expect yield --id refund-cutoff-001 --out contracts` ([`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md)) |
| Verify contracts in CI | `hotato contract verify contracts/ --junit contracts-junit.xml` |
| Attach observability traces to a contract | `hotato trace attach contracts/refund-cutoff-001.hotato --trace voice_trace.jsonl` ([`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md)) |
| Test a candidate fix, before/after, fail-closed | `hotato fix trial patch.json --name staging-x --before before/ --after after/` ([`docs/FIX-TRIAL.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-TRIAL.md)) |
| Share a finding in a PR or slide | `hotato card hotato-sweep.json#1 --out finding.svg` |
| Drive it from a coding agent | `uvx --from "hotato[mcp]" hotato-mcp` ([`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md)) |

`contract verify` and a promoted fixture in CI are two different guarantees, depending on which recording goes in:

| | On the frozen recording (every push) | On a fresh recapture (by hand, see [`docs/RECAPTURE.md`](https://github.com/attenlabs/hotato/blob/main/docs/RECAPTURE.md)) |
| --- | --- | --- |
| Proves | The evidence, policy, and scorer are intact | The CURRENT agent's behavior still matches the label |
| Does not prove | That the deployed agent has not changed | -- |

A contract bundle contains call audio. Do not commit a raw customer contract to a public repository; use sanitized fixtures for anything public. See [`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md).

## Install

Add hotato to a project with `pip`; or run any command zero-install with [`uvx`](https://docs.astral.sh/uv/):

```bash
pip install hotato                 # core: stdlib-only, zero dependencies
pip install 'hotato[neural]'       # optional Silero VAD cross-check
pip install 'hotato[transcribe]'   # optional ASR transcript, attached as context beside the score
pip install 'hotato[livekit]'      # LiveKit live capture
pip install 'hotato[pipecat]'      # Pipecat live capture
```

## Contribute a labeled call

The highest-value PR is one labeled dual-channel call. A small, high-integrity corpus of recorded calls with human-labeled turn-taking ground truth is the part of hotato that compounds: the more moments the community labels, the sharper every scorer gets. Add a clip: [`docs/SUBMITTING.md`](https://github.com/attenlabs/hotato/blob/main/docs/SUBMITTING.md) &middot; the corpus and its schema live in [`corpus/`](https://github.com/attenlabs/hotato/blob/main/corpus/README.md), with a recorded battery in [`corpus/vapi-defaults/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/vapi-defaults/README.md). Contributor guide: [`CONTRIBUTING.md`](https://github.com/attenlabs/hotato/blob/main/CONTRIBUTING.md).

## Where Hotato fits

- **Conversation QA that shows its work.** See [`docs/COMPARE.md`](https://github.com/attenlabs/hotato/blob/main/docs/COMPARE.md) for how the per-dimension scorecard fits alongside runtime voice layers.
- **Audio-timing scoring.** It scores how the conversation is timed. The opt-in `--transcribe` flag attaches an ASR transcript as context to read next to the verdict, with the timing score grounded in the audio. See [`docs/TRANSCRIBE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRANSCRIBE.md).
- **Channel-based timing.** Each channel is one party (caller on one, agent on the other); Hotato measures the timing between them, and the channels stay anonymous.
- **Timing evidence you label.** It produces candidate timing evidence; humans label intent, and CI enforces the confirmed contracts.
- **Offline and out-of-band.** It reads recordings after the call, so your live audio path and running agent stay in your hands.

## Docs

- **Set-and-forget monitoring** (connect once, sweep on a schedule, promote confirmed bugs into fixtures): [`docs/SET-AND-FORGET.md`](https://github.com/attenlabs/hotato/blob/main/docs/SET-AND-FORGET.md) &middot; runnable [`examples/set-and-forget/`](https://github.com/attenlabs/hotato/blob/main/examples/set-and-forget/README.md)
- **Bad call to CI regression test**, step by step: [`docs/BAD-CALL-TO-CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/BAD-CALL-TO-CI.md) &middot; runnable [`examples/bad-call-to-ci/`](https://github.com/attenlabs/hotato/blob/main/examples/bad-call-to-ci/README.md)
- **What it measures** (the three timing signals, re-derivable by hand): [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md) &middot; Python API [`docs/API.md`](https://github.com/attenlabs/hotato/blob/main/docs/API.md)
- **The fix ladder** (each failure names a likely fix class; when the evidence maps cleanly to stack config, Hotato names the setting family and direction): [`docs/FIX-PLANS.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-PLANS.md)
- **Rule out the non-turn-taking bugs first** (STT, buffering, verbosity, refusals, wrong-language): [`docs/WHY.md`](https://github.com/attenlabs/hotato/blob/main/docs/WHY.md)
- **Pull a call from your stack** (Vapi, Twilio, Retell, LiveKit, Pipecat): [`adapters/README.md`](https://github.com/attenlabs/hotato/blob/main/adapters/README.md) &middot; status [`docs/ADAPTER-STATUS.md`](https://github.com/attenlabs/hotato/blob/main/docs/ADAPTER-STATUS.md)
- **CI gates**: GitHub Action [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md) &middot; pytest plugin [`docs/PYTEST.md`](https://github.com/attenlabs/hotato/blob/main/docs/PYTEST.md)
- **Recorded-call battery**: 12 scripted calls against a live voice agent on its provider's default settings, where a missed interruption and a false stop on a backchannel fail in the same run, so `diagnose` surfaces the tradeoff instead of naming one threshold: [`corpus/vapi-defaults/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/vapi-defaults/README.md)
- **Failure contracts and traces**: turn a labeled candidate into a portable, CI-verified bundle and attach observability evidence: [`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md) &middot; [`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md) &middot; [`docs/OTEL.md`](https://github.com/attenlabs/hotato/blob/main/docs/OTEL.md)
- **Deterministic assertions on the transcript/trace** (phrase, PII, policy, tool-call, outcome; each scored on its own lane): [`docs/ASSERTIONS.md`](https://github.com/attenlabs/hotato/blob/main/docs/ASSERTIONS.md)
- **Checking the CURRENT agent, not just the frozen recording**: the recapture walkthrough: [`docs/RECAPTURE.md`](https://github.com/attenlabs/hotato/blob/main/docs/RECAPTURE.md)
- **Egress**: a per-command network table derived from the code, what is local, what reaches your vendor, what optional extras add a hosted call: [`docs/EGRESS.md`](https://github.com/attenlabs/hotato/blob/main/docs/EGRESS.md)
- **Layered failure evidence and a before/after fix trial**: `hotato explain` breaks a failing result down by layer, and `hotato fix trial` tests a candidate change before/after, fail-closed: [`docs/EXPLAIN.md`](https://github.com/attenlabs/hotato/blob/main/docs/EXPLAIN.md) &middot; [`docs/FIX-TRIAL.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-TRIAL.md) &middot; [`docs/APPLY.md`](https://github.com/attenlabs/hotato/blob/main/docs/APPLY.md) &middot; [`docs/FIX-LOOP.md`](https://github.com/attenlabs/hotato/blob/main/docs/FIX-LOOP.md)
- **Evidence**: what Hotato validates, the input-condition trust matrix, every card and CLI block reproducible, and where Hotato fits alongside broader voice-agent testing tools: [`docs/VALIDATION.md`](https://github.com/attenlabs/hotato/blob/main/docs/VALIDATION.md) &middot; [`docs/TRUST-MATRIX.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRUST-MATRIX.md) &middot; [`docs/GALLERY.md`](https://github.com/attenlabs/hotato/blob/main/docs/GALLERY.md) &middot; [`docs/EVIDENCE-PACK.md`](https://github.com/attenlabs/hotato/blob/main/docs/EVIDENCE-PACK.md) &middot; [`docs/COMPARE.md`](https://github.com/attenlabs/hotato/blob/main/docs/COMPARE.md)
- **For coding agents**: [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) &middot; [`llms.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms.txt) &middot; [`llms-full.txt`](https://raw.githubusercontent.com/attenlabs/hotato/main/llms-full.txt) &middot; MCP server [`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md) &middot; Security [`SECURITY.md`](https://github.com/attenlabs/hotato/blob/main/SECURITY.md)
- **Contributing**: the highest-value PR is a labeled call fixture: [`docs/SUBMITTING.md`](https://github.com/attenlabs/hotato/blob/main/docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
