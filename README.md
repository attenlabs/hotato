<p align="center">
  <a href="https://hotato.dev">
    <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato: find where your voice agent talks over callers, and keep it from coming back" width="840">
  </a>
</p>

<h1 align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/mascot.svg" alt="" width="26" align="top"> hotato
</h1>

<p align="center"><b>Find where your voice agent talks over callers, and keep it from coming back.</b></p>

<p align="center">Offline regression tests from your own call recordings. MIT.</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.9 to 3.13" src="https://img.shields.io/badge/python-3.9%20to%203.13-blue.svg">
  <img alt="offline: yes" src="https://img.shields.io/badge/offline-yes-blue.svg">
  <img alt="runtime deps: zero" src="https://img.shields.io/badge/runtime%20deps-zero-blue.svg">
  <a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/hotato-demo.gif" alt="hotato scan finding a talk-over moment in a recorded call, hotato run scoring it to a FAIL, then hotato fixture create promoting it into a permanent regression test" width="760">
</p>

One command finds every moment your agent talks over the caller:

```bash
uvx hotato scan --stereo your-call.wav   # two-channel WAV: caller ch0, agent ch1
```

From there: `run` scores a call to a PASS/FAIL verdict and an HTML report, `compare` proves a fix as a before/after delta, and `fixture create` saves any moment as a permanent regression test that fails CI if it comes back. Everything runs on your machine; the audio never leaves it.

Hotato catches the three talk-over failures callers feel: the agent talking over the caller, false-stopping on a backchannel ("mhm"), or yielding too slowly. You label the expected behavior (`yield` = stop for the caller, `hold` = keep talking through a backchannel); Hotato measures whether the timing matched. It reports what it measured, never a guess at intent.

Try it with no audio of your own:

```bash
uvx hotato demo   # scores a deliberately bad agent on synthetic audio, so you see the FAILs, timelines, and fix cards
```

## MCP

```bash
uvx --from "hotato[mcp]" hotato-mcp   # one tool, voice_eval_run; client configs in docs/MCP.md
```

## Install

`uvx hotato` runs any command with zero install. To add it to a project:

```bash
pip install hotato                 # core: stdlib-only, zero dependencies
pip install 'hotato[neural]'       # optional Silero VAD cross-check
pip install 'hotato[livekit]'      # LiveKit live capture
pip install 'hotato[pipecat]'      # Pipecat live capture
```

## Depth

- **Bad call to CI regression test**, step by step: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md) · runnable [`examples/bad-call-to-ci/`](examples/bad-call-to-ci/README.md)
- **What it measures** (the three timing signals, re-derivable by hand): [`METHODOLOGY.md`](METHODOLOGY.md) · Python API [`docs/API.md`](docs/API.md)
- **The fix ladder** (each failure names the setting to move, in your stack's own terms): [`docs/FIX-PLANS.md`](docs/FIX-PLANS.md)
- **Rule out the non-turn-taking bugs first** (STT, buffering, verbosity, refusals, wrong-language): [`docs/WHY.md`](docs/WHY.md)
- **Pull a call from your stack** (Vapi, Twilio, Retell, LiveKit, Pipecat): [`adapters/README.md`](adapters/README.md) · status [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md)
- **CI gates**: GitHub Action [`docs/CI.md`](docs/CI.md) · pytest plugin [`docs/PYTEST.md`](docs/PYTEST.md)
- **Reports and analytics**: [`docs/REPORTS.md`](docs/REPORTS.md) · Suites [`docs/SUITES.md`](docs/SUITES.md) · Stack benchmarks [`docs/BENCHMARK-STACKS.md`](docs/BENCHMARK-STACKS.md)
- **Real battery**: 12 scripted calls against a live voice agent on its provider's default settings, where a missed interruption and a false stop on a backchannel fail in the same run, so `diagnose` refuses to name one threshold: [`corpus/vapi-defaults/README.md`](corpus/vapi-defaults/README.md)
- **For agents**: [`llms.txt`](llms.txt) · [`llms-full.txt`](llms-full.txt) · MCP server [`docs/MCP.md`](docs/MCP.md) · Security [`SECURITY.md`](SECURITY.md)
- **Contributing**: the highest-value PR is a real, labelled call fixture: [`docs/SUBMITTING.md`](docs/SUBMITTING.md)

Why "hotato": good turn-taking is a game of hot potato. Speak, then pass the turn the moment the caller wants it. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
