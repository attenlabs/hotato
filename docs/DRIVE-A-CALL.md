# Drive-a-call: originate a call against a live agent, then score it

`hotato capture` scores a call you already ran. Drive-a-call closes the other
half: it PLACES the call against a live voice agent, waits for it to finish, and
feeds the recording into the same validated pull -> score pipeline. The produced
conversation is a REAL agent conversation, so it flows through scoring unchanged.

It lives in `src/hotato/drive.py` and is wired into the fleet experiment loop as
the `run_scenario` step of the Vapi and Twilio adapters
(`src/hotato/fleet/adapters.py`).

## The one honesty note that matters: the caller side is scripted, not human

The agent's half of the conversation is real. The CALLER's half is a script, and
the origin records that plainly (`origin.caller`), never claiming a human called.

- **Twilio** renders your `scenario.v1` caller script into TwiML: one `<Say>`
  per `say`-turn, `<Pause>` between them. TwiML `<Say>` speaks at **fixed
  offsets and cannot react to what the agent says** -- so this is a
  FIXED-TIMELINE regression driver: deterministic, good for catching a
  regression on a scripted turn sequence, not a reactive caller that barges in
  the instant the agent starts talking. A turn's reactive `when_agent_asks` /
  `after` label is therefore NOT honored here (it is spoken unconditionally, in
  order). A reactive caller is later work. `origin.caller = "scripted-twiml"`.
- **Vapi** originates the call FROM the assistant (the agent under test -- a
  staging clone) TO a customer number. The direction is
  outbound-from-assistant: the assistant is the party being measured, and
  whoever/whatever answers the customer number is the other side. There is no
  scripted-TwiML caller on this path. `origin.caller = "assistant-originated"`.

Either way `origin.kind = "real"`, with `origin.provider` and
`origin.provider_call_id` (the provider's own call id). This is the invariant-5
axis: a real driven call is never conflated with a simulated one, and the origin
never overstates what drove the caller side.

## The endpoints implemented

| Provider | Originate | Poll until | Then pull |
| --- | --- | --- | --- |
| Twilio | `POST /2010-04-01/Accounts/{sid}/Calls.json` with `To`/`From`/`Twiml` + `Record=true`, `RecordingChannels=dual` (the REST equivalent of `<Dial record="record-from-answer-dual">`) | `GET .../Calls/{CallSid}.json` -> `status == completed` | `GET .../Recordings.json?CallSid=...` -> `RecordingSid` -> existing `capture_twilio` (`?RequestedChannels=2`) |
| Vapi | `POST https://api.vapi.ai/call` `{assistantId, phoneNumberId, customer:{number}}` | `GET /call/{id}` -> `status == ended` | existing `capture_vapi` -> `artifact.recording.stereoUrl` |

Only `POST` (create) and `GET` (poll + pull) are ever issued -- there is no
PUT/PATCH/DELETE surface, so drive-a-call can create a call but can NEVER mutate
a provider config (an assistant, a number) in place. For Vapi the call is driven
FROM the staging CLONE, so production is untouched.

A non-`completed` Twilio call (busy / failed / no-answer / canceled) is an honest
dead-end with no recording to score -- it raises, never a fabricated verdict.

## Credentials and the egress opt-in (both required)

Placing a call reaches the provider's REST API and **costs a real phone call**.
So `run_scenario` refuses unless BOTH are present:

1. **Credentials** -- `VAPI_API_KEY`, or `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`
   (via `hotato connect` or the environment).
2. **An explicit egress opt-in** -- set `HOTATO_DRIVE_OPT_IN=1`, or pass
   `egress_opt_in: true` on the scenario. Absent it, `run_scenario` raises the
   same clean structured refusal a hosted op has always given, and dials nothing.

The drive parameters ride on the scenario (inline or under a `drive:` block) or
the environment:

- Twilio: `to_number` (the agent's number, `HOTATO_DRIVE_TO_NUMBER`),
  `from_number` (your Twilio number, `HOTATO_DRIVE_FROM_NUMBER`).
- Vapi: `phone_number_id` (`VAPI_PHONE_NUMBER_ID`), `customer_number`
  (`HOTATO_DRIVE_CUSTOMER_NUMBER`).

The recording download reuses `capture`'s validated path: http(s)-only scheme
allowlist, default-deny SSRF (a `127.0.0.1` local test recording server needs
`HOTATO_ALLOW_PRIVATE_URLS=1`, same as every other download), cross-host
credential strip, and atomic write. See [`docs/EGRESS.md`](EGRESS.md) and
[`docs/THREAT-MODEL.md`](THREAT-MODEL.md) for the per-command rows.

## What it costs and what it does not claim

- It costs one real outbound phone call per scenario run, billed by your
  provider. There is no way to drive a real agent for free; the fixed-timeline
  Twilio caller is deterministic but still a billed call.
- It does not claim the caller was human, that the scripted caller reacted to the
  agent, or that the agent "passed" -- scoring is the separate assert layer's job
  over the produced real recording.

## Retell / LiveKit / Pipecat

Retell has no confirmed create-call API, so `run_scenario` stays honestly
unadvertised for it (capture existing Retell calls with `hotato pull` instead).
LiveKit and Pipecat capture inside your own infra and have no vendor origination
API, so they are likewise not wired for drive-a-call.
