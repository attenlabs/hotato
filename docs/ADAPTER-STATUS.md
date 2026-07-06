# Adapter status

What each capture adapter is built against, verified against the vendor's live
documentation on the date shown. Auto-pull means Hotato fetches the recording
itself with your API key; capture-in-your-infra means Hotato prints the
recording config and scores the file your deployment writes. Mono recordings
are degraded input, because a single mixed channel cannot attribute overlap to
the caller or the agent; Hotato scores them only behind an explicit
`--allow-mono` / `HOTATO_ALLOW_MONO=1` opt-in and labels the result indicative
only.

| Stack | Capture mode | Current API basis | Last verified | Doc URL |
| --- | --- | --- | --- | --- |
| Vapi | auto-pull | `GET /call/{id}` -> `artifact.recording.stereoUrl` (current since the 2025-04-29 API update); falls back to deprecated `artifact.stereoRecordingUrl`, then `call.stereoRecordingUrl` | 2026-07-06 | https://docs.vapi.ai/changelog/2025/4/29 |
| Retell | auto-pull | `GET /v2/get-call/{call_id}` (Bearer) -> `scrubbed_recording_multi_channel_url` preferred, then `recording_multi_channel_url`; plain mono `recording_url` rejected unless `--allow-mono` (degraded) | 2026-07-06 | https://docs.retellai.com/api-references/get-call |
| Twilio | auto-pull | `GET .../Recordings/{RE...}.wav?RequestedChannels=2` (Basic auth); 400 when dual-channel is unavailable, then clean stop or `--allow-mono` fallback to `RequestedChannels=1` (degraded). Two-party channel order: first = customer/caller, second = agent; conferences: first = first participant to join | 2026-07-06 | https://www.twilio.com/docs/voice/api/recording |
| LiveKit | capture-in-your-infra | Two audio-only Track egresses (one per party); knobs under test: `AgentSession(turn_handling=TurnHandlingOptions(...))` with `turn_detection`, `endpointing` (`min_delay`, `max_delay`), `interruption` (`min_duration`, `min_words`, `false_interruption_timeout`, `resume_false_interruption`) | 2026-07-06 | https://docs.livekit.io/agents/logic/turns/ |
| Pipecat | capture-in-your-infra | 2-channel `AudioBufferProcessor` in-pipeline; knobs under test: `PipelineTask` user-turn strategies, e.g. `MinWordsUserTurnStartStrategy`, `VADUserTurnStartStrategy`, `KrispVivaIPUserTurnStartStrategy` (backchannel-aware), `SpeechTimeoutUserTurnStopStrategy`; `MinWordsInterruptionStrategy` deprecated since 0.0.99 | 2026-07-06 | https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies |

Every adapter validates that the file it scores has one party per channel
(2 channels) before producing separated talk-over numbers. All live fetch paths
are stdlib-only (`urllib`); stack SDKs import lazily and only inside your infra.
