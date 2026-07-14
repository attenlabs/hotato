# Drive-a-call: originate a call against a live agent, then score it

`hotato capture` scores a call you already ran. Drive-a-call closes the
other half: it PLACES the call against a live voice agent, waits for it to
finish, and feeds the recording into the same validated pull → score
pipeline. The produced conversation carries the agent's own live turns,
unscripted on that side, so it flows through scoring unchanged.

It lives in `src/hotato/drive.py` and is wired into the fleet experiment
loop as the `run_scenario` step of the Vapi and Twilio adapters
(`src/hotato/fleet/adapters.py`).

## Worth stating plainly: the caller side runs from a script

The agent's half of the conversation is unscripted. The CALLER's half runs
from a script, and the origin records that plainly as `origin.caller`.

- **Twilio** renders your `scenario.v1` caller script into TwiML: one
  `<Say>` per `say`-turn, `<Pause>` between them. TwiML `<Say>` speaks at
  **fixed offsets**, on the script's own clock regardless of what the agent
  says — a FIXED-TIMELINE regression driver, deterministic and built for
  catching a regression on a scripted turn sequence rather than a live
  back-and-forth. A turn's reactive `when_agent_asks` / `after` label is
  spoken unconditionally, in order, on this path. `origin.caller =
  "scripted-twiml"`.
- **Vapi** originates the call FROM the assistant (the agent under test — a
  staging clone) TO a customer number. The direction is
  outbound-from-assistant: the assistant is the party being measured, and
  whoever/whatever answers the customer number is the other side. There is
  no scripted-TwiML caller on this path. `origin.caller =
  "assistant-originated"`.

Either way `origin.kind = "real"`, with `origin.provider` and
`origin.provider_call_id` (the provider's own call id). This is the
invariant-5 axis: a driven call is always tagged by exactly how it was
produced, and the origin states precisely what drove the caller side.

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

Only `POST` (create) and `GET` (poll + pull) are ever issued: drive-a-call
creates a call and reads its status, and that is the whole surface — a
provider config (an assistant, a number) stays untouched. For Vapi the call
is driven FROM the staging CLONE, so production stays untouched too.

A non-`completed` Twilio call (busy / failed / no-answer / canceled) is a
dead-end with no recording to score, so it raises a clear error.

## Credentials and the egress opt-in (both required)

Placing a call reaches the provider's REST API and **costs a real phone
call**. So `run_scenario` requires BOTH before it dials:

1. **Credentials** — `VAPI_API_KEY`, or `TWILIO_ACCOUNT_SID` +
   `TWILIO_AUTH_TOKEN` (via `hotato connect` or the environment).
2. **An explicit egress opt-in** — set `HOTATO_DRIVE_OPT_IN=1`, or pass
   `egress_opt_in: true` on the scenario. Without it, `run_scenario` raises
   the same clean structured refusal a hosted op has always given, and
   dials nothing.

The drive parameters ride on the scenario (inline or under a `drive:`
block) or the environment:

- Twilio: `to_number` (the agent's number, `HOTATO_DRIVE_TO_NUMBER`),
  `from_number` (your Twilio number, `HOTATO_DRIVE_FROM_NUMBER`).
- Vapi: `phone_number_id` (`VAPI_PHONE_NUMBER_ID`), `customer_number`
  (`HOTATO_DRIVE_CUSTOMER_NUMBER`).

The recording download reuses `capture`'s validated path: http(s)-only
scheme allowlist, default-deny SSRF (a `127.0.0.1` local test recording
server needs `HOTATO_ALLOW_PRIVATE_URLS=1`, same as every other download),
cross-host credential strip, and atomic write. See
[`docs/EGRESS.md`](EGRESS.md) and [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
for the per-command rows.

## What it costs, and what the recording proves

- It costs one real outbound phone call per scenario run, billed by your
  provider — even the deterministic Twilio caller is a billed call.
- The recording captures a live agent conversation. Scoring it — whether
  the agent "passed," whether the scripted caller's timing matched a real
  one — is the separate assert layer's job, run over that produced
  recording.

## Retell / LiveKit / Pipecat

Retell calls are captured after the fact with `hotato pull`, pulling the
recording once the call has already happened. LiveKit and Pipecat calls are
captured inside your own infra — point your own capture path at the
recording, and hotato scores whatever file that infra writes.
