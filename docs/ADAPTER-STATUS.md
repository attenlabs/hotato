# Adapter status

What each capture adapter is built against, verified verbatim against the
vendor's live documentation on the date shown (integration research:
`hotato-launch/INTEGRATION-SPEC-2026-07-07.md`). This file is the map of
what Hotato can pull from each stack, and why.

Terms:

- **auto-pull**: Hotato fetches the recording itself with your API key
  (`hotato capture` / `hotato pull` / `hotato sweep`).
- **capture-in-your-infra**: there is no vendor recording API; Hotato prints
  the recording config (`hotato setup`) and scores the file your deployment
  writes.
- **mono / mixed**: a single combined channel. Attributing overlap to the
  caller or the agent needs separated tracks, so Hotato scores a combined
  channel only behind an explicit `--allow-mono` / `HOTATO_ALLOW_MONO=1`
  opt-in and labels the result indicative only. Separated turn-taking
  analysis runs on a dual-channel file.

Honesty rule (enforced): an adapter ships only with endpoints verified verbatim
in the spec. Where the spec marks a list-calls endpoint or a channel layout
**unconfirmed / none**, Hotato supports the fallback (explicit ids) and
documents the gap here.

## Build now: dual-channel (auto-pull, separated scoring)

| Stack | List recent calls | Fetch recording | Channel basis | Last verified |
| --- | --- | --- | --- | --- |
| Vapi | `GET https://api.vapi.ai/call` (params `limit`, `createdAtGt/Lt`) → JSON array of Call objects (`id`, `createdAt`) | `GET /call/{id}` → `artifact.recording.stereoUrl` (current); deprecated fallbacks `artifact.stereoRecordingUrl`, `call.stereoRecordingUrl` | `stereoUrl` is a distinct 2-channel file (customer ch0, assistant ch1) | 2026-07-07 |
| Twilio | `GET .../Accounts/{Sid}/Recordings.json` (params `PageSize`, `DateCreatedAfter/Before`, `callSid`) → `recordings[].sid` | `GET .../Recordings/{RE...}.wav?RequestedChannels=2` (HTTP Basic) | dual-channel only if the recording was created `RecordingChannels=dual`; 400 → clean stop or `--allow-mono` → `RequestedChannels=1`. Two-party order: ch0 = caller, ch1 = agent; conference: ch0 = first participant | 2026-07-07 |
| Retell | **none confirmed**: the spec marks list-calls unconfirmed; do not fabricate one. Pull from explicit `--call-id` | `GET https://api.retellai.com/v2/get-call/{id}` (Bearer) → `scrubbed_recording_multi_channel_url` preferred, then `recording_multi_channel_url`; plain mono `recording_url` rejected unless `--allow-mono` | per-party channels on the `*_multi_channel_url` fields | 2026-07-07 |
| LiveKit | **capture-in-your-infra**: `ListEgress` is an RPC method name only; no REST list. `hotato setup --stack livekit` | Two audio-only Track egresses (one per party); recording location in `egress_info.file_results[].location` | separated by running one Track egress per participant | 2026-07-07 |
| Pipecat | **capture-in-your-infra**: Pipecat Cloud session-list has no recording field; OSS has no list at all | `AudioBufferProcessor(num_channels=2)` in-pipeline (user left, bot right); you write the WAV | 2-channel in-pipeline | 2026-07-07 |

## Build now: mono / mixed only (auto-pull, `--allow-mono`, indicative only)

Each of these has a verified list + fetch, but the vendor produces a single
combined recording with **no documented per-party channel**, so scoring is
degraded and gated behind `--allow-mono`.

| Stack | List recent calls | Fetch recording | Mono basis (verbatim) | Last verified |
| --- | --- | --- | --- | --- |
| Bland AI | `GET https://api.bland.ai/v1/calls` → `calls[].call_id` | `GET /v1/calls/{id}` → `recording_url` | no stereo/channel field in the recording or call-details schema | 2026-07-07 |
| ElevenLabs Conversational AI | `GET https://api.elevenlabs.io/v1/convai/conversations` (params `page_size`, `call_start_after_unix`) → `conversations[].conversation_id` | `GET /v1/convai/conversations/{id}/audio` → combined audio | docs state the audio "does not include separate caller/agent channels, only a combined full conversation MP3" | 2026-07-07 |
| Synthflow | `GET https://api.synthflow.ai/v2/calls?model_id=…` (params `limit`, `from_date` epoch-ms) → `response.response.calls[].call_id` | `GET /v2/calls/{id}` → `response.response.calls[0].recording_url` (a Twilio Recordings URL) | Synthflow documents no dual-channel option; `recording_url` is a bare Twilio URL | 2026-07-07 |
| Millis AI | `GET {base}/call-logs` (US `api-west`, EU `api-eu-west`; param `limit`) → `histories[].session_id` | `GET /call-logs/{session_id}` → `recording.recording_url` | schema exposes only `recording_url`; `CallSettings` has only a boolean `enable_recording` | 2026-07-07 |
| Cartesia (Line) | `GET https://api.cartesia.ai/agents/calls?agent_id=…` (param `limit`; needs `Cartesia-Version` header) → `data[].id` | `GET /agents/calls/{id}/audio` → `audio/wav` | dual-vs-mono **unconfirmed** in the docs; treated as mono until a live channel-count check proves otherwise | 2026-07-07 |

`--stack synthflow` needs `--model-id` (its list endpoint requires `model_id`);
`--stack cartesia` needs `--agent-id`; `--stack millis` accepts `--base-url` for
the EU region. Single-call `capture`/`pull` by explicit id needs none of these.

## Not integrable (no vendor recording to pull)

| Stack | Why | Basis |
| --- | --- | --- |
| Deepgram Voice Agent API | Real-time WebSocket (`wss://agent.deepgram.com/v1/agent/converse`) with no REST list-calls, no fetch-recording endpoint, and no recording-ready webhook. Deepgram does not store the call. If you want a recording, capture it in your own infra (LiveKit/Pipecat pattern) | 2026-07-07, confirmed absent |
| PlayAI (formerly Play.ht) | The conversational-agent product is dead: `play.ai` DNS does not resolve and `docs.play.ai` returns `DEPLOYMENT_NOT_FOUND`. The only live domain, `docs.play.ht`, is TTS-only with no calls/recordings API | 2026-07-07, dead endpoints verified |

## Unconfirmed: needs credentials or a live probe before shipping

Documented weakly or behind a login/SPA; the spec could not verify a recording
field-path or a channel layout. Not shipped as adapters. Listed here so
nobody assumes support that was never confirmed.

- **Regal.ai**: no list-calls and no REST fetch-recording endpoint. The
  recording arrives only via the `call.recording.available` webhook's
  `properties.recording_link`, which is a literal Twilio Recordings URL. Feed
  that URL through Hotato's existing Twilio path -- the one shipped route for
  Regal-sourced recordings.
- **Thoughtly**: OpenAPI types responses as an untyped `GenericResponse.data`
  blob; no recording-URL field path could be confirmed live.
- **Sindarin**: no Sindarin-hosted recording endpoint found; `record` is
  submitted with the caller's own Twilio creds, implying the recording lands in
  the customer's own Twilio account (use the Twilio path).
- **xAI Grok Voice Agent Builder**: OpenAI-Realtime-compatible; no documented
  list-calls or fetch-recording endpoint (marketing mentions in-product
  recording, but no API contract was confirmable).
- **Cartesia dual-channel**: the `/agents/calls/{id}/audio` WAV's channel count
  was not confirmable in docs; treated as mono here pending a live check.
- **Twilio dual-channel edge behaviour**: the exact 400-on-unavailable and
  conference channel-ordering guarantees were carried forward from prior notes,
  not re-verified verbatim this pass.
- **Synthflow / Regal Twilio-URL dual-channel trick**: because their
  `recording_url` is a Twilio URL, `?RequestedChannels=2` *might* yield stereo,
  but neither vendor documents or guarantees it; unconfirmed until tested against
  a live account.

Other enterprise/partial platforms researched but not shipped (login-gated,
SPA-only docs, or transcript-only APIs): Daily, Ultravox, Hume EVI, Telnyx,
Infobip, OpenAI Realtime, Amazon Connect, Parloa, Sierra, Decagon, PolyAI,
Voiceflow, Cognigy, Dialogflow CX, Genesys Cloud, NICE CXone. See the
integration spec for the per-platform verified facts and gaps.

## Invariants

Every adapter validates that a dual-channel file it scores has one party per
channel (2 channels) before producing separated talk-over numbers; mono stacks
are scored degraded only behind `--allow-mono`. All live fetch/list paths are
stdlib-only (`urllib`); stack SDKs import lazily and only inside your own infra.
Credentials captured by `hotato connect` are stored locally at
`~/.hotato/connections.json` (mode 0600) and go straight to the vendor's own
API -- Hotato stays out of that exchange. Scoring is always offline; the only
network is the direct recording download.
