# Drive-a-call: originate a call against a live agent, then score it

`hotato capture` scores a call you already ran. Drive-a-call places the
call: it dials a live voice agent, waits for it to finish, and feeds the
recording into the same pull -> score pipeline. The agent's side is live
and unscripted, so it scores unchanged.

## The caller side runs from a script

The agent's half is unscripted. The CALLER's half runs from a script, and
the origin records that plainly as `origin.caller`:

- **Twilio** renders your `scenario.v1` caller script into TwiML: one
  `<Say>` per `say`-turn, `<Pause>` between them. `<Say>` speaks at
  **fixed offsets** on its own clock, not the agent's -- a deterministic
  driver built to catch a regression in a scripted sequence, not a live
  back-and-forth. A turn's reactive `when_agent_asks` / `after` label still
  speaks unconditionally, in order. `origin.caller = "scripted-twiml"`.
- **Vapi** originates the call FROM the assistant under test (a staging
  clone) TO a customer number: the assistant is the party being measured,
  whoever answers is the other side. `origin.caller =
  "assistant-originated"`.

Either way `origin.kind = "real"`, tagged with `origin.provider` and
`origin.provider_call_id` (the provider's own call id) -- the invariant-5
axis: every driven call states exactly how it was produced.

## The endpoints implemented

- **Twilio**
  - Originate: `POST /2010-04-01/Accounts/{sid}/Calls.json` with
    `To`/`From`/`Twiml` + `Record=true`, `RecordingChannels=dual` (the REST
    equivalent of `<Dial record="record-from-answer-dual">`).
  - Poll until: `GET .../Calls/{CallSid}.json` -> `status == completed`.
  - Then pull: `GET .../Recordings.json?CallSid=...` -> `RecordingSid` ->
    existing `capture_twilio` (`?RequestedChannels=2`).
- **Vapi**
  - Originate: `POST https://api.vapi.ai/call`
    `{assistantId, phoneNumberId, customer:{number}}`.
  - Poll until: `GET /call/{id}` -> `status == ended`.
  - Then pull: existing `capture_vapi` -> `artifact.recording.stereoUrl`.

Only `POST` (create) and `GET` (poll + pull) are issued: a provider config
stays untouched. Vapi drives the call FROM a staging CLONE, so production
stays untouched too.

A non-`completed` Twilio call (busy / failed / no-answer / canceled) has no
recording to score, so it raises a clear error.

## Credentials and the egress opt-in (both required)

Placing a call reaches the provider's REST API and bills a phone call, so
`run_scenario` requires BOTH before it dials:

1. **Credentials** -- `VAPI_API_KEY`, or `TWILIO_ACCOUNT_SID` +
   `TWILIO_AUTH_TOKEN` (via `hotato connect` or the environment).
2. **An explicit egress opt-in** -- set `HOTATO_DRIVE_OPT_IN=1`, or pass
   `egress_opt_in: true` on the scenario. Without it, `run_scenario` raises
   the same clean, structured refusal a hosted op has always given, and
   dials nothing.

Drive parameters ride on the scenario (inline or under a `drive:` block) or
the environment:

- Twilio: `to_number` (the agent's number, `HOTATO_DRIVE_TO_NUMBER`),
  `from_number` (your Twilio number, `HOTATO_DRIVE_FROM_NUMBER`).
- Vapi: `phone_number_id` (`VAPI_PHONE_NUMBER_ID`), `customer_number`
  (`HOTATO_DRIVE_CUSTOMER_NUMBER`).

The recording download reuses `capture`'s validated, SSRF-safe path. See
[`docs/EGRESS.md`](EGRESS.md) and [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
for the per-command rows.

## What it costs, and what it proves

It bills one outbound phone call per scenario run -- even the deterministic
Twilio caller is billed. The recording captures a live conversation;
whether the agent "passed," or the scripted caller's timing matched a live
one, is the separate assert layer's job.

## Retell / LiveKit / Pipecat

Retell calls are captured after the fact with `hotato pull`. LiveKit and
Pipecat calls are captured inside your own infra: point your own capture
path at the recording, and hotato scores whatever file it writes.
