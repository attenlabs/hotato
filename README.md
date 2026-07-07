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
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/hotato-demo.gif" alt="uvx hotato demo failing a bad agent, then hotato compare showing the FAIL to PASS delta" width="760">
</p>

Hotato finds the moments where your voice agent talks over the caller, turns each one into a regression test, and fails CI if it comes back. It scores a call recording on your machine, so the audio never leaves it. It catches the three failures callers feel most:

- **Talk-over**: the agent keeps talking while the caller is talking.
- **False stop**: the caller says a short acknowledgement like "mhm" (a backchannel, not a request to take over) and the agent stops mid-sentence.
- **Slow yield**: the caller starts talking and the agent takes too long to stop and let them speak.

Hotato does not infer intent. You label the expected behavior for the event: yield means the agent should stop for the caller. hold means the agent should keep speaking through a backchannel/noise/acknowledgement. Hotato then measures whether the timing matched that label.

Every failing event returns three measured signals (`did_yield`, `seconds_to_yield`, `talk_over_sec`) and a fix class. When the failure maps cleanly to stack config, the fix names the setting family and the direction to investigate; when no single threshold can win, it says so rather than inventing one.

## See it fail a bad agent

```bash
uvx hotato demo
```

![Hotato failing demo report](https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/hotato-demo-report.png)

The demo scores a deliberately bad agent on synthetic audio, so you see the FAIL verdicts, the timelines, and the fix cards before touching your own calls.

## Score your own call

```bash
uvx hotato doctor --stereo your_call.wav   # two-channel WAV: caller ch0, agent ch1
```

`doctor` scores the call, writes the visual HTML report, and opens it. Or pull a recording straight from your stack:

- **Vapi**: `uvx hotato capture --stack vapi --call-id <id>` with `VAPI_API_KEY` set.
- **Twilio**: `uvx hotato capture --stack twilio --recording-sid RE...` with `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` set.
- **Retell**: `uvx hotato capture --stack retell --call-id <id>` with `RETELL_API_KEY` set; fetches the call's multichannel recording.
- **LiveKit / Pipecat**: `uvx hotato setup --stack livekit` (or `pipecat`) prints the recording config for your infra, then `hotato capture` scores the files it produces.

Input, stated once: Hotato's main scorer requires separated caller and agent tracks: either one two-channel WAV or two aligned mono WAVs. A single mixed mono call is not enough to attribute talk-over reliably. Per-stack details and verification dates: [`adapters/README.md`](adapters/README.md), [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md).

## Turn a bad call into a regression test

```bash
hotato fixture create --stereo bad-call.wav --id refund-interruption-001 \
    --onset 42.18 --expect yield --max-talk-over 0.6 --out tests/hotato
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
```

`fixture create` clips the moment, re-bases the onset, writes the scenario label with provenance, and validates it by scoring it immediately. `hotato scan --stereo call.wav` lists candidate moments when you do not know the onset; `hotato compare --before bad.wav --after fixed.wav --onset 42.18 --expect yield` proves the fix with a FAIL to PASS delta. The full loop, step by step: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md).

## What Hotato measures

Three timing signals per event, measured frame by frame from the audio and re-derivable by hand with `--dump-frames`:

| Signal | What it answers |
| --- | --- |
| `did_yield` | did the agent stop talking after the caller started talking? |
| `seconds_to_yield` | how many seconds passed between the caller starting and the agent stopping? |
| `talk_over_sec` | for how many seconds were the caller and the agent talking at once? |

Every failing event carries exactly one fix, in one of two classes. Pass `--stack livekit`, `pipecat`, `vapi`, or `generic` and the fix uses your stack's own setting names:

| Failure | `fix_class` | The setting it names |
| --- | --- | --- |
| Missed a real interruption | `config` | interruption sensitivity: LiveKit `turn_handling` `interruption.min_duration` / `interruption.min_words`, Pipecat `VADParams(start_secs, stop_secs, confidence)`, Vapi `stopSpeakingPlan.numWords` |
| Slow yield | `config` | endpointing delay, how long the stack waits before deciding the caller is speaking or finished: LiveKit `endpointing.min_delay` / `endpointing.max_delay`, Pipecat `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)`, Vapi `stopSpeakingPlan.backoffSeconds` |
| Excess talk-over | `config` | overlap debounce: LiveKit `interruption.min_duration`, Pipecat `VADParams(stop_secs)`, Vapi `stopSpeakingPlan.voiceSeconds` |
| Yielded to its own echo | `config` | audio routing: echo cancellation and separate caller/agent channels |
| Stopped for a backchannel | `engagement-control` | no setting fixes this one: no timing threshold can tell "mhm" apart from a one-word "stop". If your stack has an interruption/backchannel classifier, use it; otherwise the fix is a learned layer that decides whether the caller is actually asking to take the turn (engagement control / addressee detection) |

Each `config` fix states which direction to move the setting and what that trades away. When one test set fails both ways at once (the agent missed a real interruption AND stopped for a backchannel), Hotato flags the pattern by name: a single sensitivity threshold trades those two failures against each other, so the fix is a classifier, not another threshold value.

## Is this even a turn-taking bug?

In our observed reports, many alleged "barge-in bugs" are not turn-taking bugs. Before
tuning a threshold, rule out: **STT hallucination** (the transcript has words
nobody said; check ASR word-error-rate, not VAD), **client-side audio
buffering** (the caller's own device queues audio before it reaches the
agent; check the jitter buffer, not interruption sensitivity), **LLM
verbosity or tool-selection** (the agent is mid-generation or mid-tool-call
and misses the stop signal; check response-length and tool-call tracing),
**safety false-refusal** (a moderation layer cuts the agent off, which looks
identical to a false stop on a backchannel; check your safety logs), and
**wrong-language STT** (recognition fails silently on a language or accent it
covers poorly, which reads as a missed interruption; check per-locale STT
accuracy). Full breakdown: [`docs/WHY.md`](docs/WHY.md#is-this-even-a-turn-taking-bug).

If your bug is not one of those five: two common complaints are
agent-talks-over-caller and false-stop-on-backchannel, and no single config
value fixes both directions at once. That is exactly what Hotato measures.

## CI

```bash
uvx hotato run --suite barge-in --format json   # text output is for humans; json is for machines
```

The bundled `barge-in` suite scores recordings of callers barging in, that is, starting to talk while the agent is talking. Exit codes: `0` every event passed (or `--no-fail`), `1` a regression, `2` a usage or IO error, or a recording that is not scorable. Not scorable means the recording cannot answer the question (the caller channel is silent, or the agent was not talking when the caller started); the event reports `scorable: false` with the reason, never an invented verdict. Two ready-made gates: copy [`.github/workflows/hotato.yml`](.github/workflows/hotato.yml) for a PR check that posts one self-updating results comment ([`docs/CI.md`](docs/CI.md)), or add `--hotato-suite` to the pytest run you already have; the plugin registers itself on install and fails the session on a regression ([`docs/PYTEST.md`](docs/PYTEST.md)).

## Real calls

`corpus/vapi-defaults/` holds 12 scripted phone calls, recorded against a live production voice assistant left on its provider's default interruption settings, dual channel, scored end to end. The battery misses a genuine interruption and false-stops on backchannels in the same run: no single sensitivity threshold fixes both directions at once, so `hotato diagnose` refuses to name one and returns `do_not_tune_single_threshold` instead.

Reproduce it: [`corpus/vapi-defaults/README.md#reproduce-it`](corpus/vapi-defaults/README.md#reproduce-it) has the single copy-paste command that re-scores the battery from a fresh clone and prints this verbatim.

![One battery, one default config, one run: a missed real interruption and a false stop on a backchannel, at once](https://raw.githubusercontent.com/attenlabs/hotato/main/corpus/vapi-defaults/both-directions-fail.png)

This is one assistant, one vendor's default configuration, one recording date, one cooperative scripted caller: a reproducible case study, not a vendor benchmark or ranking, and it carries no accuracy percentage. Recorded by the project maintainers as scripted calls; the clips and labels are MIT licensed in this repository. Full writeup, manifest, and audio: [`corpus/vapi-defaults/README.md`](corpus/vapi-defaults/README.md).

## What you get

- **`fixture create`**: one bad call moment in, one permanent regression fixture out (scenario JSON plus a clipped two-channel WAV), validated by scoring it on creation.
- **`compare`**: before/after on the same moment with one machine-stable result word (`fixed`, `regressed`, `improved`, `worse`, `unchanged`, `still_pass`, `not_scorable`).
- **`scan`**: candidate turn-taking moments across a whole recording, as timing facts only; you supply the label.
- **`diagnose` / `inspect` / `plan`**: the read-only fix ladder. Explain a finished run, read the live turn-taking config, and get a guarded one-step fix plan that is never applied automatically.
- **`doctor`**: score a recording (or the bundled self-test), write the visual report, open it.
- **`report`**: self-contained HTML with per-event SVG timelines, a per-frame inspector, print CSS for PDF, and `--base base.json` regression deltas.
- **`team`**: aggregate a directory of runs into pass rate over time plus mean/median/p90 talk-over and time-to-yield.
- **`export`**: research-grade CSVs (`events.csv`, `frames.csv`, `envelope.json`), columns documented in-file.
- **`benchmark`**: score the battery you captured through each stack, then compare the result files side by side.
- **Pytest plugin**: a `hotato_score` fixture plus a session gate (`pytest --hotato-suite`).
- **MCP server**: one tool, `voice_eval_run`; pass `report_path` to also get the HTML report. `uvx --from "hotato[mcp]" hotato-mcp`
- **Tiered corpus suites**: 112 deterministic scenarios across silver and gold tiers, plus defect suites that fail on purpose to prove the scorer catches what it claims.

## Optional neural cross-check (verified, non-reference)

`pip install 'hotato[neural]'`, then `hotato run --stereo call.wav --backend neural` recomputes the same timing signals over speech regions found by Silero VAD, a small neural speech detector, instead of the default energy threshold. The ONNX weights ship inside the package and inference runs offline on CPU. Verified properties:

- **Same contract.** The neural run returns the identical result shape as the energy reference, on the same 10 ms frame grid, end to end.
- **Deterministic.** Repeated runs on the same audio are byte-identical.
- **The energy reference is untouched.** Installing the extra changes no energy number, and `--suite` always scores with energy (a note says so if you pass `--backend neural` there).
- **Built for real recordings.** The bundled fixtures are synthetic shaped noise rendered for the energy reference; a speech-trained model finds no speech in them, so run the cross-check on your own calls, where both tracks carry real speech.
- **Clear errors.** Without the extra, `--backend neural` exits with an explicit error, never a silent fallback to energy. Silero accepts 8000 Hz, 16000 Hz, and integer multiples of 16000 Hz; other rates get an actionable resample message.

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
- Turn a bad call into a regression test with `hotato fixture create`: [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md) · runnable example: [`examples/bad-call-to-ci/`](examples/bad-call-to-ci/README.md) · hosted: [hotato.dev/docs/regression-loop.html](https://hotato.dev/docs/regression-loop.html)
- Python API: [`docs/API.md`](docs/API.md) · Stack benchmarks: [`docs/BENCHMARK-STACKS.md`](docs/BENCHMARK-STACKS.md)
- Adapters: [`adapters/README.md`](adapters/README.md) · Status and verification dates: [`docs/ADAPTER-STATUS.md`](docs/ADAPTER-STATUS.md)
- Why this exists: [`docs/WHY.md`](docs/WHY.md) · Method: [`METHODOLOGY.md`](METHODOLOGY.md) · Security: [`SECURITY.md`](SECURITY.md)
- Contributing: the highest-value PR is a real, labelled call fixture. Start at [`docs/SUBMITTING.md`](docs/SUBMITTING.md).

Why "hotato": good turn-taking is a game of hot potato. Literally: speak, then stop the moment the caller wants the turn. Hotato measures how fast and how cleanly your agent passes it. MIT licensed ([`LICENSE`](LICENSE)); the open core stays open.

mcp-name: io.github.attenlabs/hotato
