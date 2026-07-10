<p align="center">
  <a href="https://hotato.dev">
    <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato: find where your voice agent talks over callers, and pin the fix to a CI-gated contract" width="840">
  </a>
</p>

<h1 align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/mascot.svg" alt="" width="26" align="top"> hotato
</h1>

<p align="center"><b>The open-source flight recorder for production voice agents.</b></p>

<p align="center">Find where your voice agent talks over callers, and pin the fix to a portable contract with audio, timing, traces, trust checks, human labels, CI gates on every push, and verified fix trials. Recapture to prove your current agent still holds. MIT.</p>

<p align="center">
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/hotato.svg"></a>
  <a href="https://pypi.org/project/hotato/"><img alt="PyPI monthly downloads" src="https://img.shields.io/pypi/dm/hotato.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.9 to 3.13" src="https://img.shields.io/badge/python-3.9%20to%203.13-blue.svg">
  <img alt="offline: yes" src="https://img.shields.io/badge/offline-yes-blue.svg">
  <img alt="runtime deps: zero" src="https://img.shields.io/badge/runtime%20deps-zero-blue.svg">
  <a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg"></a>
</p>

## Start here (no account, no keys, no network)

```bash
uvx hotato start --demo
```

That sweeps two bundled recorded calls a provider's default agent failed, writes the dashboard, and turns one real missed-interruption candidate into a demo failure contract it immediately verifies:

```
[start] demo: swept 2 bundled calls, 5 candidate moments;
        wrote hotato-sweep.json, hotato-sweep.html, hotato-no-single-threshold.svg
        wrote contracts/demo-missed-interruption.hotato; verified contract: FAIL as expected
```

Open `hotato-sweep.html` for the ranked candidate moments with a hear-the-bug playhead. One of them is the failure below: the agent missed a real interruption in one call and false-stopped on a backchannel in another, so no single sensitivity dial fixes both. That is the card `start --demo` renders for you:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/no-single-threshold-card.svg" alt="Hotato threshold-funnel card: no single threshold can fix this. Missed a real interruption; false-stopped on a backchannel. One sensitivity dial cannot satisfy both axes at once." width="760">
</p>

A real failure became a candidate, became a portable `.hotato` contract, and `contract verify` catches it. Hotato prints the exact next commands to promote it into a permanent fixture, run it in CI, and re-verify.

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
| Prove a candidate fix, before/after, fail-closed | `hotato fix trial patch.json --name staging-x --before before/ --after after/` ([`docs/FIX-TRIAL.md`](docs/FIX-TRIAL.md)) |
| Share a finding in a PR or slide | `hotato card hotato-sweep.json#1 --out finding.svg` |
| Drive it from a coding agent | `uvx --from "hotato[mcp]" hotato-mcp` (one tool, `voice_eval_run`; configs in [`docs/MCP.md`](docs/MCP.md)) |

Every command above takes a two-channel recording (caller on one channel, agent on the other). A mono file or a bad export is marked NOT SCORABLE, never turned into a confident but meaningless verdict.

`contract verify` and a promoted fixture in CI are two different guarantees, depending on which recording goes in:

| | On the frozen recording (every push) | On a fresh recapture (by hand, see [`docs/RECAPTURE.md`](docs/RECAPTURE.md)) |
| --- | --- | --- |
| Proves | The evidence, policy, and scorer are intact | The CURRENT agent's behavior still matches the label |
| Does not prove | That the deployed agent hasn't changed | -- |

A contract bundle contains call audio. Do not commit a raw customer contract to a public repository; use sanitized fixtures for anything public. See [`docs/CONTRACTS.md`](docs/CONTRACTS.md).

## The loop

Catch it, confirm it, gate it, then prove it holds:

1. **Sweep** surfaces candidate talk-over and false-stop moments across your recent calls, ranked by how far the timing missed.
2. **You label** one (`yield` = stop for the caller, `hold` = keep talking through a backchannel) and `fixture promote` saves it as a permanent regression test.
3. **CI runs** that fixture on every change and exits non-zero if the recorded evidence stops matching your policy -- a change to the evidence, thresholds, or scorer is caught on every push. Catching the AGENT itself regressing needs a fresh recapture through the same fixture: see [`docs/RECAPTURE.md`](docs/RECAPTURE.md).

Hotato measures whether the agent stopped talking when the caller started, how many seconds that took, and how many seconds both were talking at once. It reports what it measured, never a guess at intent.

## Connect a production stack

The demo needs nothing. To point Hotato at real calls, connect once, then sweep on a schedule:

```bash
hotato connect vapi                                             # credentials stored 0600, local only
hotato sweep --stack vapi --since 7d --out hotato-sweep.html    # cron, CI, wherever
```

Run `sweep` on a timer and it becomes a passive monitor. Your audio stays on your machine unless you explicitly pull it from your stack. Full guide: [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) · runnable [`examples/set-and-forget/`](examples/set-and-forget/README.md).

## What Hotato is not

- **Not a full QA platform.** It does not grade the whole conversation, task
  success, or content. See [`docs/COMPARE.md`](docs/COMPARE.md) for where
  Hamming, Cekura, Coval, Bluejay, Roark, Vapi, and Retell fit instead.
- **Not transcript scoring.** It measures audio timing, not what was said.
- **Not speaker ID.** Channels are anonymous; nothing identifies who a person is.
- **Not semantic intent detection.** It produces candidate timing evidence.
  Humans label intent. CI enforces confirmed contracts.
- **Not a hand on production config.** It never sits in the live audio path
  and never changes a running agent.

## Install

`uvx hotato` runs any command with zero install. To add it to a project:

```bash
pip install hotato                 # core: stdlib-only, zero dependencies
pip install 'hotato[neural]'       # optional Silero VAD cross-check
pip install 'hotato[livekit]'      # LiveKit live capture
pip install 'hotato[pipecat]'      # Pipecat live capture
```

## Depth

- **Set-and-forget monitoring** (connect once, sweep on a schedule, promote confirmed bugs into fixtures): [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) · runnable [`examples/set-and-forget/`](examples/set-and-forget/README.md)
- **Bad call to CI regression test**, step by step: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md) · runnable [`examples/bad-call-to-ci/`](examples/bad-call-to-ci/README.md)
- **What it measures** (the three timing signals, re-derivable by hand): [`METHODOLOGY.md`](METHODOLOGY.md) · Python API [`docs/API.md`](docs/API.md)
- **The fix ladder** (each failure names a likely fix class; when the evidence maps cleanly to stack config, Hotato names the setting family and direction): [`docs/FIX-PLANS.md`](docs/FIX-PLANS.md)
- **Rule out the non-turn-taking bugs first** (STT, buffering, verbosity, refusals, wrong-language): [`docs/WHY.md`](docs/WHY.md)
- **Pull a call from your stack** (Vapi, Twilio, Retell, LiveKit, Pipecat): [`adapters/README.md`](adapters/README.md) · status [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md)
- **CI gates**: GitHub Action [`docs/CI.md`](docs/CI.md) · pytest plugin [`docs/PYTEST.md`](docs/PYTEST.md)
- **Recorded-call battery**: 12 scripted calls against a live voice agent on its provider's default settings, where a missed interruption and a false stop on a backchannel fail in the same run, so `diagnose` refuses to name one threshold: [`corpus/vapi-defaults/README.md`](corpus/vapi-defaults/README.md)
- **Failure contracts and traces**: turn a labelled candidate into a portable, CI-verified bundle and attach observability evidence: [`docs/CONTRACTS.md`](docs/CONTRACTS.md) · [`docs/TRACE.md`](docs/TRACE.md) · [`docs/OTEL.md`](docs/OTEL.md)
- **Proving the CURRENT agent, not just the frozen recording**: the recapture walkthrough: [`docs/RECAPTURE.md`](docs/RECAPTURE.md)
- **Egress**: a per-command network table derived from the code -- what's local, what reaches your vendor, what optional extras add a hosted call: [`docs/EGRESS.md`](docs/EGRESS.md)
- **Root cause and a proven fix**: `hotato explain` turns a failing result into root-cause-by-layer evidence, and `hotato fix trial` proves a candidate change before/after, fail-closed: [`docs/EXPLAIN.md`](docs/EXPLAIN.md) · [`docs/FIX-TRIAL.md`](docs/FIX-TRIAL.md) · [`docs/APPLY.md`](docs/APPLY.md) · [`docs/FIX-LOOP.md`](docs/FIX-LOOP.md)
- **Evidence**: what Hotato validates, the input-condition trust matrix, every card and CLI block reproducible, and where Hotato does and doesn't fit next to Hamming/Cekura/Coval/Bluejay/Roark/Vapi/Retell: [`docs/VALIDATION.md`](docs/VALIDATION.md) · [`docs/TRUST-MATRIX.md`](docs/TRUST-MATRIX.md) · [`docs/GALLERY.md`](docs/GALLERY.md) · [`docs/EVIDENCE-PACK.md`](docs/EVIDENCE-PACK.md) · [`docs/COMPARE.md`](docs/COMPARE.md)
- **For coding agents**: [`AGENTS.md`](AGENTS.md) · [`llms.txt`](llms.txt) · [`llms-full.txt`](llms-full.txt) · MCP server [`docs/MCP.md`](docs/MCP.md) · Security [`SECURITY.md`](SECURITY.md)
- **Contributing**: the highest-value PR is a labelled call fixture: [`docs/SUBMITTING.md`](docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
