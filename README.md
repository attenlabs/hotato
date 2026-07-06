<p align="center">
  <a href="https://hotato.dev">
    <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/banner.png" alt="hotato: turn-taking regression tests for voice agents" width="840">
  </a>
</p>

<h1 align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/mascot.svg" alt="" width="26" align="top"> hotato
</h1>

<p align="center"><b>Find interruption bugs in your voice agent before users do.</b></p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.9 to 3.13" src="https://img.shields.io/badge/python-3.9%20to%203.13-blue.svg">
  <img alt="offline: yes" src="https://img.shields.io/badge/offline-yes-blue.svg">
  <img alt="runtime deps: zero" src="https://img.shields.io/badge/runtime%20deps-zero-blue.svg">
  <a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg"></a>
</p>

Hotato scores turn-taking from a call recording, on your machine, so call audio stays with you.
It catches the three failures callers feel most: the agent talks over a real interruption, stops
for a backchannel ("mhm"), or is slow to yield. Every failing event returns three measured
signals (`did_yield`, `seconds_to_yield`, `talk_over_sec`) and a fix class that names the knob to turn.

## See it fail a bad agent

```bash
uvx hotato demo
```

![Hotato failing demo report](https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/hotato-demo-report.png)

The demo battery is intentionally bad and fully synthetic: it exists to show what a catch looks like.

## Score your own call

```bash
uvx hotato doctor --stereo your_call.wav   # two-channel WAV: caller ch0, agent ch1
```

`doctor` scores the call, writes the visual HTML report, and opens it. Or pull a recording straight from your stack:

- **Vapi**: `uvx hotato capture --stack vapi --call-id <id>` with `VAPI_API_KEY` set.
- **Twilio**: `uvx hotato capture --stack twilio --recording-sid RE...` with `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` set.
- **Retell**: `uvx hotato capture --stack retell --call-id <id>` with `RETELL_API_KEY` set; fetches the call's multichannel recording.
- **LiveKit / Pipecat**: `uvx hotato setup --stack livekit` (or `pipecat`) scaffolds recording in your infra, then `hotato capture` scores the files.

Scope, stated once: Hotato scores separated caller/agent tracks, as one two-channel WAV or two aligned mono WAVs. Per-stack details and verification dates: [`adapters/README.md`](adapters/README.md), [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md).

## What Hotato measures

Three objective timing signals per event, measured frame by frame and reproducible with `--dump-frames`:

| Signal | What it answers |
| --- | --- |
| `did_yield` | did the agent stop talking when the caller took the floor? |
| `seconds_to_yield` | how long did that take? |
| `talk_over_sec` | how many seconds it kept talking over the caller first |

Every failing event carries exactly one fix, from a taxonomy of two classes. Pass `--stack livekit`, `pipecat`, `vapi`, or `generic` to get the knob in your stack's vocabulary:

| Failure | `fix_class` | The knob it names |
| --- | --- | --- |
| Missed a real interruption | `config` | interruption sensitivity: LiveKit `turn_handling` `interruption.min_duration` / `interruption.min_words`, Pipecat `VADParams(start_secs, stop_secs, confidence)`, Vapi `stopSpeakingPlan.numWords` |
| Slow yield | `config` | endpointing latency: LiveKit `endpointing.min_delay` / `endpointing.max_delay`, Pipecat `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)`, Vapi `stopSpeakingPlan.backoffSeconds` |
| Excess talk-over | `config` | overlap debounce: LiveKit `interruption.min_duration`, Pipecat `VADParams(stop_secs)`, Vapi `stopSpeakingPlan.voiceSeconds` |
| Yielded to its own echo | `config` | audio routing: echo cancellation and separate caller/agent channels |
| Yielded to a backchannel | `engagement-control` | a vendor-neutral pointer. No single timing threshold separates a backchannel from a one-word interruption. Where your stack provides an interruption/backchannel classifier, use it; the general case calls for a learned engagement-control / addressee-detection layer |

Each `config` knob ships with a direction and its honest trade-off. When a battery fails on both axes at once (missed a real interruption AND false-triggered on a backchannel), a battery-level funnel pointer fires: that pattern is the signal a discriminating layer is needed, since one threshold trades the two cases against each other.

## CI

```bash
uvx hotato run --suite barge-in --format json   # text is the human default; json is the machine envelope
```

Exit codes: `0` all pass (or `--no-fail`), `1` a regression, `2` usage or IO error, or a single recording that is not scorable (silent caller, or agent silent at onset: the event reports `scorable: false` with the reason, never a fake verdict). Two ready-made gates: copy [`.github/workflows/hotato.yml`](.github/workflows/hotato.yml) for a PR check with a sticky results comment ([`docs/CI.md`](docs/CI.md)), or add `--hotato-suite` to your existing pytest run; the plugin auto-registers on install and fails the session on a regression ([`docs/PYTEST.md`](docs/PYTEST.md)).

## What you get

- **`doctor`**: score a recording (or the bundled self-test), write the visual report, open it.
- **`report`**: self-contained HTML with per-event SVG timelines, a per-frame inspector, print CSS for PDF, and `--base base.json` regression deltas.
- **`team`**: aggregate a directory of runs into pass rate over time plus mean/median/p90 talk-over and time-to-yield.
- **`export`**: research-grade CSVs (`events.csv`, `frames.csv`, `envelope.json`), columns documented in-file.
- **`benchmark`**: score your captured battery per stack, then compare result files side by side.
- **Pytest plugin**: a `hotato_score` fixture plus a session gate (`pytest --hotato-suite`).
- **MCP server**: one tool, `voice_eval_run`; pass `report_path` to also get the HTML report. `uvx --from "hotato[mcp]" hotato-mcp`
- **Tiered corpus suites**: 112 deterministic scenarios across silver and gold tiers, plus defect suites that must fail.

## Optional neural cross-check (verified, non-reference)

`pip install 'hotato[neural]'`, then `hotato run --stereo call.wav --backend neural` re-runs the same turn-taking timing math over a Silero VAD speech track. The ONNX weights ship inside the package and inference runs offline on CPU. Verified properties:

- **Same contract.** The neural track comes back in the identical result shape as the energy reference, on the same hop grid, end to end.
- **Deterministic.** Repeated runs on the same audio are byte-identical.
- **The energy reference is untouched.** Installing the extra changes no energy number, and `--suite` always scores with energy (a note says so if you pass `--backend neural` there).
- **Built for real recordings.** The bundled fixtures are synthetic shaped noise rendered for the energy reference, and a speech-trained model marks no speech in them, so the cross-check is informative on your own calls, where both tracks carry real speech.
- **Clear errors.** Without the extra, `--backend neural` exits with an explicit error, never a silent energy fallback. Silero accepts 8000 Hz, 16000 Hz, and integer multiples of 16000 Hz; other rates get an actionable resample message.

Full method and the measured cross-check properties: [`METHODOLOGY.md`](METHODOLOGY.md).

## Install

`uvx hotato` runs every command with zero install. To add it to a project:

```bash
pip install hotato                 # core: stdlib-only, zero dependencies
pip install 'hotato[neural]'       # optional Silero VAD cross-check
pip install 'hotato[livekit]'      # LiveKit live capture
pip install 'hotato[pipecat]'      # Pipecat live capture
```

## More

- Reports and analytics: [`docs/REPORTS.md`](docs/REPORTS.md) · Pytest gate: [`docs/PYTEST.md`](docs/PYTEST.md) · Suites: [`docs/SUITES.md`](docs/SUITES.md) · CI recipes: [`docs/CI.md`](docs/CI.md)
- Turn a bad call into a regression test: [hotato.dev/docs/regression-loop.html](https://hotato.dev/docs/regression-loop.html)
- Python API: [`docs/API.md`](docs/API.md) · Stack benchmarks: [`docs/BENCHMARK-STACKS.md`](docs/BENCHMARK-STACKS.md)
- Adapters: [`adapters/README.md`](adapters/README.md) · Status and verification dates: [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md)
- Why this exists: [`docs/WHY.md`](docs/WHY.md) · Method: [`METHODOLOGY.md`](METHODOLOGY.md) · Security: [`SECURITY.md`](SECURITY.md)
- Contributing: the highest-value PR is a real, labelled call fixture. Start at [`docs/SUBMITTING.md`](docs/SUBMITTING.md).

Why "hotato": good turn-taking is a game of hot potato. Take your turn, then pass it, fast and clean. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
