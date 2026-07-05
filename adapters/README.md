# Capture adapters — score your OWN call

The fastest way to use Hotato is not the synthetic self-test — it is to point it
at a **real dual-channel call from your stack** and get a scored verdict. Each
adapter does one thing: get a two-channel recording (caller on channel 0, agent
on channel 1) and run it through the same scorer the bundled battery uses.

Two entry points, same logic (single-sourced in `hotato.capture`, so they never
drift):

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
| **Vapi** (flagship) | Downloads the call's stereo recording. **API key only, no SDK.** | `hotato capture --stack vapi --call-id <id>` |
| **Twilio** | Downloads a **dual-channel** recording (`RecordingChannels=dual`). | `hotato capture --stack twilio --recording-sid RE...` |
| **LiveKit** | You run two-track **Egress** in your deployment → two mono WAVs. | `hotato setup --stack livekit`, then `--caller a.wav --agent b.wav` |
| **Pipecat** | A drop-in **2-channel `AudioBufferProcessor`** records the session. | `hotato setup --stack pipecat`, then `--stereo captured.wav` |
| **Retell** | **Honest:** no confirmed self-serve stereo export — workaround only. | `hotato setup --stack retell` |

`--stack {vapi,twilio,livekit,pipecat,retell}`. Every stack also accepts an
already-captured file via `--stereo file.wav` (caller on ch0) or
`--caller a.wav --agent b.wav`, so you can score any recording you already have.

### Vapi — near-zero friction (the hero)

```bash
export VAPI_API_KEY=<your private key>
hotato capture --stack vapi --call-id <call-id> --expect yield
```

Under the hood: `GET https://api.vapi.ai/call/<id>` → `artifact.stereoRecordingUrl`
(a 2-channel WAV, customer on channel 0, assistant on channel 1) → scored offline.
No SDK, no export step; the only network is the direct download from Vapi to your
machine.

### Twilio — turn on dual-channel first

```bash
# 1) record dual-channel:  <Record recordingChannels="dual"> / record-from-answer-dual
# 2) then:
export TWILIO_ACCOUNT_SID=AC...  TWILIO_AUTH_TOKEN=...
hotato capture --stack twilio --recording-sid RE... --expect yield
```

`GET .../Accounts/<sid>/Recordings/<RE...>.wav` (HTTP Basic auth). Twilio's channel
order depends on how the recording was made; if caller/agent look swapped, add
`--caller-channel/--agent-channel`.

### LiveKit — capture two tracks, then score

`hotato setup --stack livekit` prints an Egress scaffold. RoomComposite mixes both
parties into one channel and cannot attribute overlap, so run **two audio-only
Track egresses** (one per participant), convert to WAV, and score:

```bash
hotato capture --stack livekit --caller caller.wav --agent agent.wav --onset <sec>
```

`adapters/livekit_capture.py` also carries an inline `AgentSession` live-capture
template with three `# ADJUST:` points if you prefer to record in-process.

### Pipecat — a 2-channel recorder you drop in

`hotato setup --stack pipecat` prints a drop-in `AudioBufferProcessor(num_channels=2)`
recorder that writes `[caller, agent]` as a two-channel WAV. Then:

```bash
hotato capture --stack pipecat --stereo captured.wav --expect yield
```

`adapters/pipecat_capture.py` has the same recorder wired into a live pipeline
template.

### Retell — honest status

No confirmed self-serve **stereo / dual-channel** export was found. Retell's
`GET /v2/get-call/<id>` returns a single (mono/mixed) `recording_url`, and a mono
mix cannot attribute overlap to caller vs agent. `hotato setup --stack retell`
prints the workaround (capture dual-channel at the telephony layer you control, or
score a dual-channel WAV you assembled). We do not fake a capture path that does
not exist — if Retell adds a stereo export, open an issue and we'll add a
first-class adapter.

## One-command demo (zero install, zero deps, no network)

Prove the capture → score loop before you wire anything. `--demo` copies a bundled
two-channel reference and runs it straight through the scorer:

```bash
hotato capture --stack vapi   --demo
hotato capture --stack twilio --demo
hotato capture --stack livekit --demo
hotato capture --stack pipecat --demo
hotato capture --stack retell --demo
# or the standalone file:  PYTHONPATH=src python adapters/vapi_capture.py --demo
```

Each prints the three timing signals and a verdict, and exits `0`. No stack SDK,
no account, no network.

## The two-channel requirement (the one thing that matters)

The scorer measures energy **per channel**. It can only separate "the agent talked
over the caller" from "the caller talked over the agent" when the two are on
**separate channels**:

- **channel 0 = caller**, **channel 1 = agent**.
- Turn on **dual-channel / stereo / separate-track** recording wherever you
  configure it. Most stacks and telephony providers can.
- A **mono-mixed** export (both parties summed into one channel) cannot attribute
  overlap to the right party. It degrades every number — it is *not* a drop-in
  substitute. Keep the two parties split all the way to the WAV.

## Install the optional stack

The core scorer, plus Vapi and Twilio capture, are **stdlib-only** (HTTP via
`urllib` + your API key) — nothing to install. The **live** LiveKit/Pipecat
capture path needs your framework, imported lazily and only on the live path:

```bash
pip install 'hotato[livekit]'   # livekit + livekit-agents
pip install 'hotato[pipecat]'   # pipecat-ai
# vapi / twilio need no extra dependency:  uvx 'hotato[vapi]' resolves to core.
```

## What the scorer does *not* claim

Same honest scope as the rest of the tool: it measures the **timing** of
turn-taking (`did_yield`, `seconds_to_yield`, `talk_over_sec`) and nothing else.
**No accuracy percentage. Energy is not intent.** No speaker identification, no
diarization, no transcription, no emotion/intent detection. None of these adapters
has been run against a live stack in this build — the live paths use the documented
APIs and are marked for verification on your side. See the top-level `README.md`
and `METHODOLOGY.md` for the method and its ceiling.
