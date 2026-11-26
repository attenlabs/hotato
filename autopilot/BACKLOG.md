# Backlog

Goal: every capability that voice-agent teams already rely on in testing and monitoring tools.
Ranked. The builder takes the first item not marked `[x]`. The weekly scorecard may reorder it.

- [x] Production call monitoring plus alerts (Slack, PagerDuty, webhook): `hotato monitor`
- [x] AI-judged criteria next to the timing metrics: `hotato test run` persona cases with quoted evidence and pass rates
- [x] Dashboards and a team workspace, self-hosted with docker compose: `hotato cloud`

1. Voice-mode persona tests: run a `hotato test run` case over a real call (Vapi web call or Twilio, reusing `hotato drive`) so turn timing and the criteria are scored from the same call
2. Import existing test suites from Retell, ElevenLabs and Vapi into `hotato test` case files
3. OpenTelemetry: accept OTLP protobuf as well as JSON, follow the GenAI semantic conventions, and show per-call cost and latency in `hotato cloud`
4. Human review in `hotato cloud`: label a call or a verdict, and report how often the AI judge agrees with people
5. Tune the judge prompt against labelled verdicts and report the agreement before and after
6. Red-team and compliance case packs: caller prompt injection, PII disclosure, recording consent
7. Persona-test targets for LiveKit and Pipecat agents; publish the TypeScript SDK
8. Load tests started and read from `hotato cloud`
9. Instant shareable first result: one uploaded call becomes a report link
