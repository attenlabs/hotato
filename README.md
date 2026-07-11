<p align="center">
  <a href="https://hotato.dev">
    <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato: find where your voice agent talks over callers, and pin the fix to a CI-gated contract" width="840">
  </a>
</p>

<h1 align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/mascot.svg" alt="" width="26" align="top"> hotato
</h1>

<p align="center"><b>The open-source flight recorder for production voice agents.</b></p>

<p align="center">
  Catch where it talks over callers, pin the failure to a portable contract, and re-check it in CI.<br>
  Offline by default &middot; MIT &middot; zero runtime dependencies.
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

## Quickstart -- no account, no keys, no network

```bash
uvx hotato start --demo
```

It sweeps two bundled recorded calls a provider's default agent failed, writes the candidate dashboard, and turns one missed-interruption candidate into a demo failure contract it immediately verifies:

```
[start] demo: swept 2 bundled calls, 5 candidate moments;
        wrote hotato-sweep.json, hotato-sweep.html, hotato-no-single-threshold.svg
        wrote contracts/demo-missed-interruption.hotato; verified contract: FAIL as expected
```

Open `hotato-sweep.html`. This is the actual output -- the ranked candidate moments, each with a hear-the-bug playhead:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/sweep-dashboard.png" alt="The hotato candidate dashboard: 5 candidate moments ranked by salience across 2 calls, each card showing the caller and agent timeline, an embedded hear-the-bug audio playhead, and promote-as-yield / promote-as-hold / ignore buttons." width="820">
</p>
<p align="center"><sub><code>hotato-sweep.html</code> &middot; candidate moments ranked by salience, each with a playhead that sweeps the timeline in sync with the embedded audio. Candidates you review and label, never a decided verdict.</sub></p>

`start --demo` promoted one of these candidates into a portable `.hotato` contract and ran `contract verify` on it (FAIL, as expected). It then prints the exact next commands to save the candidate as a permanent fixture, gate it in CI, and re-check it.

Point it at your own recording to walk the same loop end to end:

```bash
hotato start --stereo my-call.wav          # trust -> scan -> review -> label -> contract
```

Every command takes a two-channel recording (caller on one channel, agent on the other). A mono file or a bad export is marked NOT SCORABLE, never turned into a confident but meaningless verdict.

## How the loop works

Catch a moment, confirm what it should have done, gate it in CI, then re-check today's agent.

### 1. Catch -- surface the candidate moments

`sweep` ranks the talk-over and false-stop moments across your recent calls by how far the timing missed. Two candidate types, straight from the open scorer, no accuracy score:

<table align="center">
<tr>
<td width="412"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/talk-over-card.svg" alt="Level 1 candidate card: 0.32s of overlap while the agent was talking, at t=2s in the recording. Hotato reports timing candidates, not intent." width="400"></td>
<td width="412"><img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/false-stop-card.svg" alt="Level 1 candidate card: 0.46s of silence after the agent stopped with no caller nearby, at t=1.28s in the recording. Hotato reports timing candidates, not intent." width="400"></td>
</tr>
<tr>
<td colspan="2" align="center"><sub><b>Level 1 -- candidate.</b> Hotato reports what it measured (0.32s of overlap; 0.46s of trailing silence), never a guess at intent. Each is a timing moment worth review, not a bug yet.</sub></td>
</tr>
</table>

### 2. Confirm -- you label yield or hold

You decide the expected behavior for a candidate: `yield` (stop for the caller) or `hold` (keep talking through a backchannel). No single sensitivity dial decides it for you -- a threshold that stops missing interruptions starts false-stopping on backchannels, so when both fail in one run `diagnose` refuses to name one threshold:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/no-single-threshold-card.svg" alt="No single threshold can fix this: one sensitivity dial cannot both avoid missing a real interruption and avoid false-stopping on a backchannel. Hotato refused threshold tuning; fix class engagement-control." width="760">
</p>
<p align="center"><sub><b>Level 2 -- human-labeled failure.</b> A reviewer confirms the recording broke an explicit yield-or-hold expectation. When a missed interruption and a false stop collide, the fix is the engagement-control class, not one dial.</sub></p>

### 3. Gate -- pin it to a CI contract

`fixture promote` saves your labeled call as a permanent regression test. On every push, CI re-scores that recording under the pinned thresholds and scorer, and exits non-zero if the stored evidence stops matching your policy:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/report-scored.png" alt="The scored hotato HTML report: 0 of 1 events pass with a REGRESSION verdict, an analytics panel (time to yield, talk-over histogram, failure clusters by fix class), and the per-event timeline with caller and agent channels, the measured metrics, and the fix note." width="820">
</p>
<p align="center"><sub><b>Level 3 -- stored-evidence check.</b> One recording, the pinned scorer, a FAIL against the labeled yield expectation. The same audio and config reproduce every number in this report.</sub></p>

### 4. Prove -- re-check the current agent

The frozen recording catches the evidence, thresholds, or scorer drifting. To check that the CURRENT agent still behaves, recapture the same scenario and score a NEW contract under the same policy:

```bash
# place the same call against today's agent, capture dual-channel, then:
hotato contract create --stereo fresh-call.wav --onset 41.90 --expect yield \
    --id refund-cutoff-001-recapture --out contracts
hotato contract verify contracts/refund-cutoff-001-recapture.hotato
```

<p align="center"><sub><b>Level 4 -- fresh-recapture comparison.</b> A newly captured call meets the same labelled policy and no submitted paired guard regressed. This is the claim the frozen-recording gate cannot make. Walkthrough: <a href="docs/RECAPTURE.md"><code>docs/RECAPTURE.md</code></a>.</sub></p>

## Five levels of evidence

Hotato never calls the weakest level a verdict. Every card, report, and CLI result names its level; the public tier is the weakest one its inputs support, never a blended score.

| Level | Name | What it means |
| --- | --- | --- |
| 1 | Candidate | A timing moment worth human review. Not a bug yet. |
| 2 | Human-labeled failure | A reviewer confirmed this recording broke an explicit yield-or-hold expectation. |
| 3 | Stored-evidence check | The historical audio still produces the expected result under the pinned policy and scorer. |
| 4 | Fresh-recapture comparison | A newly captured call passed the same contract, and no submitted paired guard regressed. |
| 5 | External proof | An independent team confirmed a caught regression or a fresh recapture. Not yet published. |

A before/after experiment (`hotato fix trial`, and the fleet loop) re-derives every verdict from the on-disk audio under one pinned trial manifest. It refuses a proof built from an edited verdict, a re-encoded old call, a dropped fixture, or unrelated audio. The number comes from re-scoring the recordings every time, never from a stored field.

## Connect a production stack

The demo needs nothing. To point Hotato at real calls, connect once, then sweep on a schedule:

```bash
hotato connect vapi                                             # credentials stored 0600, local only
hotato sweep --stack vapi --since 7d --out hotato-sweep.html    # cron, CI, wherever
```

Run `sweep` on a timer and it becomes a scheduled batch scanner. Your audio stays on your machine unless you explicitly pull it from your stack. Full guide: [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) &middot; runnable [`examples/set-and-forget/`](examples/set-and-forget/README.md).

## Fleet -- private, self-hosted

`hotato fleet` runs the loop across every agent from one local workspace: ingest calls, surface candidates, label them, and run a before/after experiment that recomputes both sides from audio under a pinned manifest. It recommends a change; it never deploys one. Local mode is stdlib-only (SQLite plus a content-addressed store) with no account and no hosted dependency, and no product limit on how many agents you register.

```bash
hotato fleet init    -w acme
hotato fleet agent add -w acme --name support-bot --stack vapi --assistant-id asst_123
hotato fleet ingest  -w acme --agent support-bot call.wav
hotato fleet discover -w acme --agent support-bot call.wav
hotato fleet review   -w acme
```

A before/after experiment refuses a proof built from an edited verdict, a re-encoded old call, a dropped fixture, or unrelated audio: the number comes from re-scoring the recordings, under one pinned policy, every time. Full guide: [`docs/GUARDIAN-FLEET.md`](docs/GUARDIAN-FLEET.md).

## Choose your path

| You want to | Run this |
| --- | --- |
| Try the full loop, no credentials | `uvx hotato start --demo` |
| Sweep the bundled demo calls | `uvx hotato sweep --demo` |
| Sweep recent calls from a real stack | `hotato connect vapi` then `hotato sweep --stack vapi --since 7d` |
| Add Hotato to an existing repo, CI gate included | `hotato init starter --stack vapi --out .` ([`docs/STARTER.md`](docs/STARTER.md)) |
| Turn a confirmed failure into a portable contract | `hotato contract create --from-candidate hotato-sweep.json#1 --expect yield --id refund-cutoff-001 --out contracts` ([`docs/CONTRACTS.md`](docs/CONTRACTS.md)) |
| Verify contracts in CI | `hotato contract verify contracts/ --junit contracts-junit.xml` |
| Attach observability traces to a contract | `hotato trace attach contracts/refund-cutoff-001.hotato --trace voice_trace.jsonl` ([`docs/TRACE.md`](docs/TRACE.md)) |
| Test a candidate fix, before/after, fail-closed | `hotato fix trial patch.json --name staging-x --before before/ --after after/` ([`docs/FIX-TRIAL.md`](docs/FIX-TRIAL.md)) |
| Share a finding in a PR or slide | `hotato card hotato-sweep.json#1 --out finding.svg` |
| Drive it from a coding agent | `uvx --from "hotato[mcp]" hotato-mcp` (the `voice_eval_run` scorer + eleven fleet tools; configs in [`docs/MCP.md`](docs/MCP.md)) |

`contract verify` and a promoted fixture in CI are two different guarantees, depending on which recording goes in:

| | On the frozen recording (every push) | On a fresh recapture (by hand, see [`docs/RECAPTURE.md`](docs/RECAPTURE.md)) |
| --- | --- | --- |
| Proves | The evidence, policy, and scorer are intact | The CURRENT agent's behavior still matches the label |
| Does not prove | That the deployed agent hasn't changed | -- |

A contract bundle contains call audio. Do not commit a raw customer contract to a public repository; use sanitized fixtures for anything public. See [`docs/CONTRACTS.md`](docs/CONTRACTS.md).

## Install

`uvx hotato` runs any command with zero install. To add it to a project:

```bash
pip install hotato                 # core: stdlib-only, zero dependencies
pip install 'hotato[neural]'       # optional Silero VAD cross-check
pip install 'hotato[livekit]'      # LiveKit live capture
pip install 'hotato[pipecat]'      # Pipecat live capture
```

## What Hotato is not

- **Not a full QA platform.** It does not grade the whole conversation, task success, or content -- it isolates turn-taking timing and pins it to reproducible evidence. See [`docs/COMPARE.md`](docs/COMPARE.md) for how it fits alongside broader voice-agent testing tools.
- **Not transcript scoring.** It measures audio timing, not what was said.
- **Not speaker ID.** Channels are anonymous; nothing identifies who a person is.
- **Not semantic intent detection.** It produces candidate timing evidence. Humans label intent. CI enforces confirmed contracts.
- **Not a hand on production config.** It never sits in the live audio path and never changes a running agent.

## Docs

- **Set-and-forget monitoring** (connect once, sweep on a schedule, promote confirmed bugs into fixtures): [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) &middot; runnable [`examples/set-and-forget/`](examples/set-and-forget/README.md)
- **Bad call to CI regression test**, step by step: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md) &middot; runnable [`examples/bad-call-to-ci/`](examples/bad-call-to-ci/README.md)
- **What it measures** (the three timing signals, re-derivable by hand): [`METHODOLOGY.md`](METHODOLOGY.md) &middot; Python API [`docs/API.md`](docs/API.md)
- **The fix ladder** (each failure names a likely fix class; when the evidence maps cleanly to stack config, Hotato names the setting family and direction): [`docs/FIX-PLANS.md`](docs/FIX-PLANS.md)
- **Rule out the non-turn-taking bugs first** (STT, buffering, verbosity, refusals, wrong-language): [`docs/WHY.md`](docs/WHY.md)
- **Pull a call from your stack** (Vapi, Twilio, Retell, LiveKit, Pipecat): [`adapters/README.md`](adapters/README.md) &middot; status [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md)
- **CI gates**: GitHub Action [`docs/CI.md`](docs/CI.md) &middot; pytest plugin [`docs/PYTEST.md`](docs/PYTEST.md)
- **Recorded-call battery**: 12 scripted calls against a live voice agent on its provider's default settings, where a missed interruption and a false stop on a backchannel fail in the same run, so `diagnose` refuses to name one threshold: [`corpus/vapi-defaults/README.md`](corpus/vapi-defaults/README.md)
- **Failure contracts and traces**: turn a labelled candidate into a portable, CI-verified bundle and attach observability evidence: [`docs/CONTRACTS.md`](docs/CONTRACTS.md) &middot; [`docs/TRACE.md`](docs/TRACE.md) &middot; [`docs/OTEL.md`](docs/OTEL.md)
- **Checking the CURRENT agent, not just the frozen recording**: the recapture walkthrough: [`docs/RECAPTURE.md`](docs/RECAPTURE.md)
- **Egress**: a per-command network table derived from the code -- what's local, what reaches your vendor, what optional extras add a hosted call: [`docs/EGRESS.md`](docs/EGRESS.md)
- **Layered failure evidence and a before/after fix trial**: `hotato explain` breaks a failing result down by layer, and `hotato fix trial` tests a candidate change before/after, fail-closed: [`docs/EXPLAIN.md`](docs/EXPLAIN.md) &middot; [`docs/FIX-TRIAL.md`](docs/FIX-TRIAL.md) &middot; [`docs/APPLY.md`](docs/APPLY.md) &middot; [`docs/FIX-LOOP.md`](docs/FIX-LOOP.md)
- **Evidence**: what Hotato validates, the input-condition trust matrix, every card and CLI block reproducible, and where Hotato fits alongside broader voice-agent testing tools: [`docs/VALIDATION.md`](docs/VALIDATION.md) &middot; [`docs/TRUST-MATRIX.md`](docs/TRUST-MATRIX.md) &middot; [`docs/GALLERY.md`](docs/GALLERY.md) &middot; [`docs/EVIDENCE-PACK.md`](docs/EVIDENCE-PACK.md) &middot; [`docs/COMPARE.md`](docs/COMPARE.md)
- **For coding agents**: [`AGENTS.md`](AGENTS.md) &middot; [`llms.txt`](llms.txt) &middot; [`llms-full.txt`](llms-full.txt) &middot; MCP server [`docs/MCP.md`](docs/MCP.md) &middot; Security [`SECURITY.md`](SECURITY.md)
- **Contributing**: the highest-value PR is a labelled call fixture: [`docs/SUBMITTING.md`](docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
