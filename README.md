<p align="center">
  <a href="https://hotato.dev">
    <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato: find where your voice agent talks over callers, and keep it from coming back" width="840">
  </a>
</p>

<h1 align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/mascot.svg" alt="" width="26" align="top"> hotato
</h1>

<p align="center"><b>Find where your voice agent talks over callers, and keep it from coming back.</b></p>

<p align="center">Offline turn-taking regression tests from your own call recordings. MIT.</p>

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

That sweeps two bundled recorded calls a provider's default agent failed and writes three files, offline:

```
[start] demo: swept 2 bundled calls, 5 candidate moments;
        wrote hotato-sweep.json, hotato-sweep.html, hotato-no-single-threshold.svg
```

Open `hotato-sweep.html` for the ranked candidate moments with a hear-the-bug playhead. One of them is the failure below: the agent missed a real interruption in one call and false-stopped on a backchannel in another, so no single sensitivity dial fixes both. That is the card `start --demo` renders for you:

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/cards/no-single-threshold-card.svg" alt="Hotato threshold-funnel card: no single threshold can fix this. Missed a real interruption; false-stopped on a backchannel. One sensitivity dial cannot satisfy both axes at once." width="760">
</p>

Prefer a single self-contained dashboard and nothing else? `uvx hotato sweep --demo --out hotato-sweep.html`.

## Choose your path

| You want to | Run this |
| --- | --- |
| See it work with zero setup | `uvx hotato start --demo` |
| Scan one call recording you already have | `uvx hotato scan --stereo call.wav` |
| Monitor a production stack (Vapi, Twilio, Retell, LiveKit, Pipecat) | `hotato connect vapi` then `hotato sweep --stack vapi --since 7d` |
| Add Hotato to an existing repo, CI gate included | `hotato init starter --stack vapi --out .` ([`docs/STARTER.md`](docs/STARTER.md)) |
| Turn a confirmed bug into a portable, CI-enforced contract | `hotato contract create --from-candidate hotato-sweep.json#1 --expect yield --id refund-cutoff-001 --out contracts` ([`docs/CONTRACTS.md`](docs/CONTRACTS.md)) |
| Turn a confirmed bug into a CI test | `hotato fixture promote hotato-sweep.json#1 --expect yield --out tests/hotato` |
| Prove a fix held across the battery | `hotato verify --before before.json --after after.json` |
| Share a finding in a PR or slide | `hotato card hotato-sweep.json#1 --out finding.svg` |
| Drive it from a coding agent | `uvx --from "hotato[mcp]" hotato-mcp` (one tool, `voice_eval_run`; configs in [`docs/MCP.md`](docs/MCP.md)) |

`scan` takes a two-channel WAV (caller on channel 0, agent on channel 1). A mono file or a bad export is marked NOT SCORABLE, never turned into a confident but meaningless verdict.

## The loop

Catch it, confirm it, gate it, keep it gone:

1. **Sweep** surfaces candidate talk-over and false-stop moments across your recent calls, ranked by how far the timing missed.
2. **You label** one (`yield` = stop for the caller, `hold` = keep talking through a backchannel) and `fixture promote` saves it as a permanent regression test.
3. **CI runs** that fixture on every change and exits non-zero the moment the timing regresses, so the bug a caller already felt cannot come back unnoticed.

Hotato measures whether the agent stopped talking when the caller started, how many seconds that took, and how many seconds both were talking at once. It reports what it measured, never a guess at intent.

## Connect a production stack

The demo needs nothing. To point Hotato at real calls, connect once, then sweep on a schedule:

```bash
hotato connect vapi                                             # credentials stored 0600, local only
hotato sweep --stack vapi --since 7d --out hotato-sweep.html    # cron, CI, wherever
```

Run `sweep` on a timer and it becomes a passive monitor. Your audio stays on your machine unless you explicitly pull it from your stack. Full guide: [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) · runnable [`examples/set-and-forget/`](examples/set-and-forget/README.md).

## What Hotato is not

- **Not a live guardrail.** It scores recordings after the call, offline. It never sits in your production audio path.
- **Not an intent reader.** It measures timing; you label the expected behavior. It surfaces candidate moments, not verdicts about what the agent meant.
- **Not a scorecard.** There is no accuracy percentage anywhere. Every verdict is a timing that re-runs identically on any machine, quoted with the command that produced it.
- **Not mono-first.** It needs the caller and agent on separate channels. A single mixed track is marked NOT SCORABLE.
- **Not an uploader.** Nothing leaves your machine unless you pull it from your own stack.

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
- **For coding agents**: [`AGENTS.md`](AGENTS.md) · [`llms.txt`](llms.txt) · [`llms-full.txt`](llms-full.txt) · MCP server [`docs/MCP.md`](docs/MCP.md) · Security [`SECURITY.md`](SECURITY.md)
- **Contributing**: the highest-value PR is a labelled call fixture: [`docs/SUBMITTING.md`](docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
