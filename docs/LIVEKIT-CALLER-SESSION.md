# Direct LiveKit caller session

`hotato.livekit_session.LiveKitCallerSession` is an optional transport for the
bounded scripted, hybrid, generative, and frozen-replay caller engine. It joins
a LiveKit room as a participant, publishes PCM16LE through one audio track,
receives the target participant's audio and transcription events, and submits
SIP DTMF through the official Python RTC SDK.

This adapter removes the custom WebSocket sidecar requirement for a LiveKit
agent. It does not implement SIP, a carrier, STT, TTS, packet impairment, or a
telephony transfer service. A PSTN test still needs LiveKit SIP and a configured
trunk. LiveKit documents `publish_dtmf` as publishing a SIP DTMF message; a
successful SDK call is not evidence that a downstream carrier honored it.

Official transport references:

- [LiveKit Python SDK](https://github.com/livekit/python-sdks)
- [Python RTC API](https://docs.livekit.io/reference/python/livekit/rtc/index.html)
- [AudioSource queue and playout contract](https://docs.livekit.io/reference/python/livekit/rtc/audio_source.html)
- [AudioStream bounded-capacity option](https://docs.livekit.io/reference/python/livekit/rtc/audio_stream.html)

## Install and run

```bash
pip install 'hotato[livekit]'
```

Mint a short-lived room token with only the permissions needed by the caller
participant. Keep token generation outside the test plan and result package.

The CLI can join the room directly and synthesize caller turns with a local
Piper model. The token enters through an environment variable or regular file,
never a command-line value:

```bash
export LIVEKIT_CALLER_TOKEN="$(mint-short-lived-room-token)"

hotato caller run scenarios/refund-barge-in.json \
  --livekit-url wss://livekit.example.test \
  --livekit-target-identity agent-under-test \
  --livekit-token-env LIVEKIT_CALLER_TOKEN \
  --livekit-evidence-topic hotato.evidence.v1 \
  --piper-model models/en_US-lessac-medium.onnx \
  --piper-config models/en_US-lessac-medium.onnx.json \
  --allow-remote \
  --out artifacts/refund-barge-in
```

`--livekit-token-file` is the file-backed alternative. `--target-ws` remains
the mutually exclusive sidecar transport. The CLI derives the LiveKit audio
rate from `audio.sample_rate` in the Piper config; an explicit
`--livekit-sample-rate` must match it.

```python
import os

from hotato.caller import load_plan, run_caller
from hotato.livekit_session import LiveKitCallerSession

# Your TTS adapter must return Hotato's documented PCM + provenance mapping.
from my_test_adapters import LocalTTS

session = LiveKitCallerSession(
    os.environ["LIVEKIT_URL"],
    os.environ["LIVEKIT_CALLER_TOKEN"],
    target_identity="agent-under-test",
    allow_remote=True,                  # explicit network-egress decision
    sample_rate_hz=48_000,              # TTS output must match exactly
    evidence_topic="hotato.evidence.v1",
)

try:
    run = run_caller(
        load_plan("scenarios/refund-barge-in.json"),
        session,
        "artifacts/refund-barge-in",
        tts=LocalTTS(sample_rate_hz=48_000),
    )
    print(run.result["status"], session.media_summary())
finally:
    session.close()
```

`send_text` is `UNSUPPORTED`: a LiveKit data message is not spoken caller
audio. Supply a TTS adapter so caller `say` and model-generated `say` actions
produce PCM. Sample-rate conversion is also explicit; the transport refuses a
TTS result whose sample rate differs from the configured `AudioSource`.

## Evidence semantics

The adapter records each layer without promoting one into another:

| Event | What it establishes | Authority |
|---|---|---|
| `audio_submitted` | `AudioSource.capture_frame` accepted every frame and `wait_for_playout` completed | local LiveKit SDK |
| `received_audio_frame` | decoded PCM reached the Hotato caller participant | local LiveKit receiver |
| `delivered_audio_receipt` | a cooperating target participant reported the bytes at its named boundary | target participant reported |
| `dtmf_submitted` | the local participant's `publish_dtmf` calls completed | local LiveKit SDK |
| native transcription | LiveKit emitted a transcription event for the target identity | LiveKit room event |

Every received audio frame includes its SHA-256 and a rolling SHA-256 over the
ordered PCM stream. `media_summary()` returns only digests and counters. Raw PCM
remains in the caller engine's content-addressed package when the scenario and
TTS lane write it there.

`evidence()` binds that digest-only media summary and the five evidence
capability states into `caller-result.json.session_boundary`. It contains no
room token, endpoint query, raw audio, transcript text, or target payload.

`sdk_playout_complete` does not establish target, SIP, PSTN, or carrier
delivery. `evidence_capabilities()["outgoing_audio_delivery"]` remains
`UNOBSERVABLE` until a valid, session-bound target receipt arrives.

## Optional target evidence channel

Set `evidence_topic` only when the target participant implements this strict
envelope on reliable LiveKit data messages:

```json
{
  "schema": "hotato.livekit-evidence.v1",
  "session_id": "<session id from the control message>",
  "sequence": 1,
  "kind": "audio_receipt",
  "payload": {
    "submission_sequence": 1,
    "submitted_sha256": "sha256:<announced caller PCM digest>",
    "delivered_sha256": "sha256:<PCM digest measured at the target boundary>",
    "delivered_bytes": 19200,
    "sample_rate_hz": 48000,
    "channels": 1,
    "boundary": "agent-input-after-decode"
  }
}
```

The default control topic is `hotato.control.v1`. The adapter sends
`session_started` and `audio_submission` messages only to `target_identity`.
The target copies the session id and announced submission digest into its
receipt, then supplies its separately measured delivered digest. Receipt
sequence must be contiguous. Duplicate, reordered, wrong-session, malformed,
oversized, and wrong-submission receipts become
`target_evidence_rejected` events; their payload is represented by a digest,
not copied into the result.

The same evidence envelope accepts `transcript`, `tool_result`,
`state_snapshot`, `transfer`, `hold`, `timing`, and `custom`. These remain
`target_participant_reported`. They can supply evidence to Hotato's later
deterministic assertions; this transport never produces a pass/fail verdict.

## Resource and failure behavior

- Remote egress is refused unless `allow_remote=True`; loopback remains the
  default.
- The URL refuses embedded credentials, query strings, and fragments.
- Before publishing the caller track, the driver applies LiveKit track
  subscription permissions that allow only `target_identity`. Control and
  evidence data messages are also addressed only to that identity.
- The short-lived token is passed to the SDK during connection and never copied
  into the facade, control messages, event queue, `repr`, or media summary. The
  SDK necessarily retains connection credentials while its room is connected.
- PCM per send, PCM per session, per-send duration, event count, remote track
  count, control message size, frame duration, connection time, operation time,
  and close time are bounded.
- Caller media operations are serialized. A concurrent audio/DTMF call is
  refused instead of interleaving frames from two scenario actions.
- The SDK's remote `AudioStream` uses a nonzero capacity. The SDK's behavior at
  that internal capacity boundary remains transport behavior to measure. The
  separate Hotato event queue is bounded and its overflow fails the run instead
  of silently dropping evidence.
- Driver errors expose the operation and exception type without copying SDK
  messages that may contain endpoint details.
- The adapter has no hidden retry. Load scheduling and retry policy belong to
  the load controller, where attempts remain observable.

## Acceptance gate before a transport claim

The unit suite validates protocol mapping against a fake SDK. Publish a LiveKit
transport result only after an environment-specific run captures:

1. pinned LiveKit server, SIP server, Python SDK, agent, STT, TTS, and carrier
   versions;
2. the caller package and rolling received-stream digest;
3. a target-boundary receipt, if target delivery is claimed;
4. SIP participant state and carrier evidence for PSTN claims;
5. network capture and egress destinations;
6. disconnect, reconnect, event-overflow, token-expiry, and media-timeout cases.

Until that acceptance run exists, describe this as an implemented optional
LiveKit caller transport with fake-SDK contract tests, not as a measured
carrier-grade path.
