# Adapter status

Which stack Hotato pulls recordings from, which endpoint it calls, and how it
gets separated caller/agent channels out of each one. Every entry is checked
verbatim against the vendor's live documentation on the date shown
(integration research: `hotato-launch/INTEGRATION-SPEC-2026-07-07.md`).

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

Each adapter ships with only the endpoints confirmed verbatim in the spec.
Where the spec marks a list-calls endpoint or a channel layout **unconfirmed
/ none**, Hotato falls back to explicit call ids and documents the gap here.

## Dual-channel: auto-pull, separated scoring

One entry per stack, verified 2026-07-07 unless noted otherwise. Each of
these auto-pulls a recording with per-party channels, so Hotato scores full
separated turn-taking without `--allow-mono`.

- **Vapi**
  - List recent calls: `GET https://api.vapi.ai/call` (params `limit`, `createdAtGt/Lt`) → JSON array of Call objects (`id`, `createdAt`)
  - Fetch recording: `GET /call/{id}` → `artifact.recording.stereoUrl` (current); deprecated fallbacks `artifact.stereoRecordingUrl`, `call.stereoRecordingUrl`
  - Channel basis: `stereoUrl` is a distinct 2-channel file (customer ch0, assistant ch1)
- **Twilio**
  - List recent calls: `GET .../Accounts/{Sid}/Recordings.json` (params `PageSize`, `DateCreatedAfter/Before`, `callSid`) → `recordings[].sid`
  - Fetch recording: `GET .../Recordings/{RE...}.wav?RequestedChannels=2` (HTTP Basic)
  - Channel basis: dual-channel only if the recording was created `RecordingChannels=dual`; 400 → clean stop or `--allow-mono` → `RequestedChannels=1`. Two-party order: ch0 = caller, ch1 = agent; conference: ch0 = first participant
- **Retell**
  - List recent calls: **none confirmed** -- the spec marks list-calls unconfirmed; do not fabricate one. Pull from explicit `--call-id`
  - Fetch recording: `GET https://api.retellai.com/v2/get-call/{id}` (Bearer) → `scrubbed_recording_multi_channel_url` preferred, then `recording_multi_channel_url`; plain mono `recording_url` rejected unless `--allow-mono`
  - Channel basis: per-party channels on the `*_multi_channel_url` fields
- **LiveKit**
  - List recent calls: **capture-in-your-infra** -- `ListEgress` is an RPC method name only; no REST list. `hotato setup --stack livekit`
  - Fetch recording: two audio-only Track egresses (one per party); recording location in `egress_info.file_results[].location`
  - Channel basis: separated by running one Track egress per participant
- **Pipecat**
  - List recent calls: **capture-in-your-infra** -- Pipecat Cloud session-list has no recording field; OSS has no list at all
  - Fetch recording: `AudioBufferProcessor(num_channels=2)` in-pipeline (user left, bot right); you write the WAV
  - Channel basis: 2-channel in-pipeline

## Mono / mixed: auto-pull, `--allow-mono`, indicative only

Each of these has a verified list + fetch, but the vendor produces a single
combined recording with **no documented per-party channel**. Hotato scores it
indicative-only, behind `--allow-mono`. One entry per stack, verified
2026-07-07.

- **Bland AI**
  - List recent calls: `GET https://api.bland.ai/v1/calls` → `calls[].call_id`
  - Fetch recording: `GET /v1/calls/{id}` → `recording_url`
  - Mono basis (verbatim): no stereo/channel field in the recording or call-details schema
- **ElevenLabs Conversational AI**
  - List recent calls: `GET https://api.elevenlabs.io/v1/convai/conversations` (params `page_size`, `call_start_after_unix`) → `conversations[].conversation_id`
  - Fetch recording: `GET /v1/convai/conversations/{id}/audio` → combined audio
  - Mono basis (verbatim): docs state the audio "does not include separate caller/agent channels, only a combined full conversation MP3"
- **Synthflow**
  - List recent calls: `GET https://api.synthflow.ai/v2/calls?model_id=…` (params `limit`, `from_date` epoch-ms) → `response.response.calls[].call_id`
  - Fetch recording: `GET /v2/calls/{id}` → `response.response.calls[0].recording_url` (a Twilio Recordings URL)
  - Mono basis (verbatim): Synthflow documents no dual-channel option; `recording_url` is a bare Twilio URL
- **Millis AI**
  - List recent calls: `GET {base}/call-logs` (US `api-west`, EU `api-eu-west`; param `limit`) → `histories[].session_id`
  - Fetch recording: `GET /call-logs/{session_id}` → `recording.recording_url`
  - Mono basis (verbatim): schema exposes only `recording_url`; `CallSettings` has only a boolean `enable_recording`
- **Cartesia (Line)**
  - List recent calls: `GET https://api.cartesia.ai/agents/calls?agent_id=…` (param `limit`; needs `Cartesia-Version` header) → `data[].id`
  - Fetch recording: `GET /agents/calls/{id}/audio` → `audio/wav`
  - Mono basis (verbatim): dual-vs-mono **unconfirmed** in the docs; treated as mono until a live channel-count check proves otherwise

`--stack synthflow` needs `--model-id` (its list endpoint requires `model_id`);
`--stack cartesia` needs `--agent-id`; `--stack millis` accepts `--base-url` for
the EU region. Single-call `capture`/`pull` by explicit id needs none of these.

## No vendor recording to pull

- **Deepgram Voice Agent API** (2026-07-07, confirmed absent) -- real-time
  WebSocket (`wss://agent.deepgram.com/v1/agent/converse`), with no REST
  list-calls, fetch-recording endpoint, or recording-ready webhook: the call
  itself is never stored vendor-side. Capture the recording in your own
  infra instead (the LiveKit/Pipecat pattern above).
- **PlayAI (formerly Play.ht)** (2026-07-07, dead endpoints verified) -- the
  conversational-agent product is retired: `play.ai` DNS does not resolve and
  `docs.play.ai` returns `DEPLOYMENT_NOT_FOUND`. The only live domain,
  `docs.play.ht`, is TTS-only with no calls/recordings API.

## Needs credentials or a live probe before shipping

Documented weakly or behind a login/SPA; the spec could not verify a recording
field-path or a channel layout for these, so they ship as fallbacks (explicit
call ids or the vendor's own webhook URL) rather than full adapters. Listed
here so the supported set stays exact.

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

Also researched, login-gated or SPA-only docs or transcript-only APIs: Daily,
Ultravox, Hume EVI, Telnyx, Infobip, OpenAI Realtime, Amazon Connect, Parloa,
Sierra, Decagon, PolyAI, Voiceflow, Cognigy, Dialogflow CX, Genesys Cloud,
NICE CXone. The integration spec carries the per-platform verified facts and
gaps.

## What holds across every adapter

Every adapter validates that a dual-channel file it scores has one party per
channel (2 channels) before producing separated talk-over numbers; mono stacks
score degraded, only behind `--allow-mono`. Every live fetch/list path is
stdlib-only (`urllib`); stack SDKs import lazily, and only inside your own
infra. Credentials captured by `hotato connect` sit locally at
`~/.hotato/connections.json` (mode 0600) and go straight from your machine to
the vendor's own API, with Hotato out of that exchange. Scoring runs offline;
the only network call is the direct recording download.
