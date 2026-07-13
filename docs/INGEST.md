# `hotato ingest` -- the composable passive on-ramp

Wire a webhook to invoke `hotato ingest` once, and every completed call is
scanned for **candidate** turn-taking moments automatically.

`ingest` surfaces timing candidates for you to review. You decide which ones
matter and promote them to a permanent regression test with
`hotato fixture create` -- the yield/hold label always comes from you.

It is built by **composition** and adds only a per-stack webhook parser:

```
parse the webhook payload   ->  extract the call id / recording locator
fetch the recording         ->  hotato.capture (the SAME fetch the adapters use)
scan for candidates         ->  hotato.scan (no labels, no verdict)
write a candidate report     ->  JSON always; --out an HTML report (optional)
```

## How it runs

- **You own the trigger.** Hotato ships the command; wire it to a webhook
  handler, a serverless function, or a cron over your call log, and it runs
  offline and self-hosted, on your infrastructure. The only network call is
  the same recording fetch `hotato capture` already makes.
- **You own the label.** A candidate is a timing event. Only you know whether
  a caller sound was "mhm" or "stop", so the label is yours.

## Contract

```
hotato ingest --stack {vapi|retell|twilio|livekit|pipecat} \
    (--event PAYLOAD.json | --call-id ID | --recording-sid RE...) \
    [--out report.html] [--format text|json] [--allow-mono] [--top N] [--min-gap S]
```

- **Exit 0** = ran (candidates reported, possibly zero).
- **Exit 2** = parse / fetch / IO error, or not-scorable input (for example a mono
  recording, which cannot attribute overlap to caller vs agent). Never a pass/fail.

`--format` controls stdout (`text` listing or the `json` candidate list, capped by
`--top`). `--out` additionally writes an HTML candidate report containing every
candidate. A webhook payload is **untrusted DATA**: `ingest` reads only the named
locator fields out of it.

## Wire your webhook -> `hotato ingest`

Point your platform's call-completed webhook at a small handler that saves the
payload and shells out to `ingest`. The pattern is identical for every stack:

```python
# a minimal webhook handler (framework-agnostic pseudocode)
import json, subprocess, tempfile, os

def on_call_completed(request):
    payload = request.body                       # the platform's webhook payload
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False) as fh:
        fh.write(payload)
        event_path = fh.name
    # ingest is one process; run it out-of-band so the webhook returns fast.
    subprocess.Popen([
        "hotato", "ingest", "--stack", "vapi",
        "--event", event_path,
        "--out", f"candidates/{request_id}.html",
    ], env={**os.environ, "VAPI_API_KEY": os.environ["VAPI_API_KEY"]})
    return 200
```

A cron over your call log works equally well when you would rather batch:

```bash
# nightly: scan yesterday's calls for candidates
for id in $(your-call-log --since yesterday --ids); do
  hotato ingest --stack vapi --call-id "$id" \
      --format json --out "candidates/$id.html" >> candidates/$id.json
done
```

### Per-stack recipe

Webhook field paths verified against live vendor docs (2026-07-07). Where a field
could not be confirmed from the live docs, `ingest` parses it **defensively** (a
missing field is simply absent, never fabricated).

| Stack | Webhook | Field ingest reads | Credentials for the fetch |
|-------|---------|--------------------|---------------------------|
| **vapi** | end-of-call-report | `message.call.id` (confirmed) | `VAPI_API_KEY` |
| **retell** | call webhook | top-level `event` + `call.call_id` (confirmed) | `RETELL_API_KEY` |
| **twilio** | `recordingStatusCallback` | `RecordingSid` (confirmed; form-encoded body) | `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` |
| **livekit** | egress webhook | `egressInfo.fileResults[].location` / `.filename` (defensive) | none -- egress lands in your storage |
| **pipecat** | your own event | `recording_path` / `recording_url` (defensive) | none -- you produced the file |

For **vapi / retell / twilio**, `ingest` extracts the identifier from the payload
and delegates the dual-channel recording fetch to the same adapter `hotato capture`
uses (which already resolved the recording URLs; see
[ADAPTER-STATUS.md](ADAPTER-STATUS.md)) -- the recording URL always comes from
that validated fetch, not the raw payload.

For **livekit / pipecat**, the recording lands in *your* infra, so the event
carries the locator directly. LiveKit egress files land in your storage bucket;
supply a `recording_url` (downloaded) or a `recording_path` (read locally). A
Pipecat event is whatever you emit, for example:

```json
{ "recording_path": "captured.wav" }
```

You can also skip the payload entirely with a direct id:

```bash
hotato ingest --stack vapi   --call-id  <id>   --out candidates.html   # + VAPI_API_KEY
hotato ingest --stack twilio --recording-sid RE... --format json       # + TWILIO_*
hotato ingest --stack pipecat --event event.json                        # local file
```

## From a candidate to a regression test

`ingest` finds the moment; you decide what should have happened and freeze it:

```bash
# 1. ingest surfaces a candidate at t=42.18s in a call recording
# 2. you listen, decide the agent should have yielded, and promote it:
hotato fixture create --stereo call.wav --onset 42.18 \
    --expect yield --id refund-cutoff-001 --out tests/hotato
# 3. it is now a permanent test:
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
```

See [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md) for the full bad-call-to-CI loop.

## Notes on `--allow-mono`

Discovery needs one party per channel to attribute overlap. `--allow-mono` (or
`HOTATO_ALLOW_MONO=1`) lets the *fetch* pull a mono-only recording on retell/twilio
(matching `hotato capture`), but a mono mix is still reported **not-scorable**
(exit 2) for discovery, because overlap cannot be split between caller and agent.
Record dual-channel to get candidates.
