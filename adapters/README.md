# Capture adapters: score your own call

The fastest path to real value: point Hotato at a real dual-channel call from your
stack. Each adapter does one thing: fetch a two-channel recording (caller on
channel 0, agent on channel 1) and run it through the same scorer the bundled
battery uses.

Two entry points, one shared implementation (single-sourced in `hotato.capture`):

```bash
# installed (recommended)
hotato capture --stack vapi --call-id <id>          # + VAPI_API_KEY
hotato setup   --stack livekit                       # scaffold the recording config

# or run the standalone adapter file from a checkout
PYTHONPATH=src python adapters/vapi_capture.py --call-id <id>
```

## One command per stack

| Stack | How Hotato gets the two channels | Command |
| --- | --- | --- |
| **Vapi** (flagship) | Downloads the call's stereo recording. API key only. | `hotato capture --stack vapi --call-id <id>` |
| **Retell** | Downloads the call's multi-channel recording. API key only. | `hotato capture --stack retell --call-id <id>` |
| **Twilio** | Downloads the dual-channel media (`.wav?RequestedChannels=2`). | `hotato capture --stack twilio --recording-sid RE...` |
| **LiveKit** | Two-track Egress in your deployment writes two mono WAVs. | `hotato setup --stack livekit`, then `--caller a.wav --agent b.wav` |
| **Pipecat** | A drop-in 2-channel `AudioBufferProcessor` records the session. | `hotato setup --stack pipecat`, then `--stereo captured.wav` |

`--stack {vapi,twilio,livekit,pipecat,retell}`. Every stack also accepts an
already-captured file via `--stereo file.wav` (caller on ch0) or
`--caller a.wav --agent b.wav`, so you can score any recording you already have.

### Vapi: near-zero friction

```bash
export VAPI_API_KEY=<your private key>
hotato capture --stack vapi --call-id <call-id> --expect yield
```

Under the hood: `GET https://api.vapi.ai/call/<id>` returns
`artifact.recording.stereoUrl` (a 2-channel WAV, customer on channel 0, assistant
on channel 1; the current field since Vapi's 2025-04-29 API update), scored
offline. Hotato falls back to the deprecated `artifact.stereoRecordingUrl` and
`call.stereoRecordingUrl` for older payloads. The only network is the direct
download from Vapi to your machine.

### Retell: near-zero friction

```bash
export RETELL_API_KEY=<your api key>
hotato capture --stack retell --call-id <call-id> --expect yield
```

Under the hood: `GET https://api.retellai.com/v2/get-call/<call-id>` (Bearer auth)
returns per-party recordings once the call ends. Hotato prefers
`scrubbed_recording_multi_channel_url` (PII scrubbed), falls back to
`recording_multi_channel_url`, and validates the downloaded WAV has exactly
2 channels. The plain mono `recording_url` is rejected by default because a mono
mix cannot attribute talk-over to caller vs agent; `--allow-mono` (adapter) or
`HOTATO_ALLOW_MONO=1` (CLI) opts into scoring it as clearly degraded.

### Twilio: turn on dual-channel first

```bash
# 1) record dual-channel:  <Record recordingChannels="dual"> / record-from-answer-dual
# 2) then:
export TWILIO_ACCOUNT_SID=AC...  TWILIO_AUTH_TOKEN=...
hotato capture --stack twilio --recording-sid RE... --expect yield
```

`GET .../Accounts/<sid>/Recordings/<RE...>.wav?RequestedChannels=2` (HTTP Basic
auth); appending `?RequestedChannels=2` is Twilio's documented way to request the
dual-channel media. If dual-channel is unavailable Twilio returns `400 Bad
Request` and Hotato stops with a clear message (mono cannot attribute talk-over)
unless you pass `--allow-mono`. Channel order for two-party calls: first/left
channel = customer/caller, second/right = agent, which matches Hotato's default
caller=ch0 agent=ch1. Conference recordings put the first participant to join on
the first channel; if caller and agent look swapped, add `--caller-channel` /
`--agent-channel`.

### LiveKit: capture two tracks, then score

`hotato setup --stack livekit` prints an Egress scaffold. RoomComposite mixes both
parties into one channel, so run two audio-only Track egresses (one per
participant), convert to WAV, and score:

```bash
hotato capture --stack livekit --caller caller.wav --agent agent.wav --onset <sec>
```

`adapters/livekit_capture.py` also carries an inline `AgentSession` live-capture
template with three `# ADJUST:` points for recording in-process.

### Pipecat: a 2-channel recorder you drop in

`hotato setup --stack pipecat` prints a drop-in
`AudioBufferProcessor(num_channels=2)` recorder that writes `[caller, agent]` as a
two-channel WAV. Then:

```bash
hotato capture --stack pipecat --stereo captured.wav --expect yield
```

`adapters/pipecat_capture.py` has the same recorder wired into a live pipeline
template.

## One-command demo (offline)

Prove the capture-then-score loop before wiring anything. `--demo` copies a bundled
two-channel reference and runs it through the scorer:

```bash
hotato capture --stack vapi   --demo
hotato capture --stack twilio --demo
hotato capture --stack livekit --demo
hotato capture --stack pipecat --demo
hotato capture --stack retell --demo
# or the standalone file:  PYTHONPATH=src python adapters/vapi_capture.py --demo
```

Each prints the three timing signals and a verdict, and exits `0`.

## Two channels are ground truth

The scorer measures energy per channel, so it can tell "the agent talked over the
caller" from "the caller talked over the agent" when the two are on separate
channels:

- **channel 0 = caller**, **channel 1 = agent**.
- Turn on dual-channel / stereo / separate-track recording wherever you configure
  it. Most stacks and telephony providers support it.
- Keep the two parties split all the way to the WAV, and every overlap number
  stays authoritative.

## Install the optional stack

The core scorer, plus Vapi, Retell and Twilio capture, are stdlib-only (HTTP via
`urllib` and your API key). The live LiveKit and Pipecat capture path needs your
framework, imported lazily and only on the live path:

```bash
pip install 'hotato[livekit]'   # livekit + livekit-agents
pip install 'hotato[pipecat]'   # pipecat-ai
# vapi, retell and twilio need no extra dependency:  uvx 'hotato[vapi]' resolves to core.
```

## Scope

Same as the rest of the tool: it measures the timing of turn-taking (`did_yield`,
`seconds_to_yield`, `talk_over_sec`) from speech energy over time. Each adapter's
exact API basis and last-verified date: [`docs/ADAPTER-STATUS.md`](../docs/ADAPTER-STATUS.md).
See the top-level `README.md` and `METHODOLOGY.md` for the method.
