# Connect, pull, sweep: score every call across your stack

`hotato sweep` connects once and surfaces every turn-taking problem across
your calls, in three steps you can also run alone:

1. **`hotato connect <stack>`** -- store a stack's credentials once, locally.
2. **`hotato pull`** -- bulk-fetch your recent recordings into a folder.
3. **`hotato sweep`** -- pull, then run zero-config `analyze` and write one
   offline dashboard of the ranked turn-taking moments.

Everything scores offline; the only network call is the direct recording
download from vendor to machine. Your audio and keys stay between you and
the vendor.

## 1. Connect once

```
hotato connect vapi --api-key <key>
```

Runs a live auth check (lists one recent call), then writes the
credentials to `~/.hotato/connections.json` (mode `0600`, dir `0700`). The
key never prints and reaches only the vendor's API. Credentials also fall
back to the environment:

```
VAPI_API_KEY=<key> hotato connect vapi
```

Per-stack credentials:

| Stack | Flags (or env vars) |
| --- | --- |
| vapi | `--api-key` / `VAPI_API_KEY` |
| retell | `--api-key` / `RETELL_API_KEY` |
| twilio | `--account-sid` `--auth-token` / `TWILIO_ACCOUNT_SID` `TWILIO_AUTH_TOKEN` |
| bland | `--api-key` / `BLAND_API_KEY` |
| elevenlabs | `--api-key` / `ELEVENLABS_API_KEY` |
| synthflow | `--api-key` / `SYNTHFLOW_API_KEY` (+ `--model-id` / `SYNTHFLOW_MODEL_ID` to list) |
| millis | `--api-key` / `MILLIS_API_KEY` (+ `--base-url` for the EU region) |
| cartesia | `--api-key` / `CARTESIA_API_KEY` (+ `--agent-id` / `CARTESIA_AGENT_ID` to list) |

Retell has no list endpoint, so `connect retell` stores the key and
validates it on first pull. `--no-verify` skips the live check for any
stack. LiveKit and Pipecat capture in your own infra: connect with `hotato
setup --stack livekit|pipecat` instead.

Once connected, `--stack` and credential flags are optional for `pull` and
`sweep` when exactly one stack is connected.

## 2. Pull recent recordings

```
hotato pull --stack vapi --since 7d --limit 50
hotato pull                                   # the only connected stack
```

Lists recent recordings via the vendor's verified endpoint, then downloads
each into `hotato-pull-<stack>/` (override `--out DIR`). `--since` accepts
`7d`, `12h`, `30m`, `2w`. A recording that fails to fetch is a clean skip
with its reason; the pull continues.

**Retell has no verified list endpoint.** Pull it from explicit ids
instead:

```
hotato pull --stack retell --call-id c1 --call-id c2
```

For Twilio, `--call-id` values are Recording SIDs (`RE...`).

**Mono / mixed stacks** (bland, elevenlabs, synthflow, millis, cartesia)
produce one combined recording with no per-party channel; attributing
talk-over needs `--allow-mono`, and results stay indicative only:

```
hotato pull --stack bland --allow-mono --limit 20
```

## 3. Sweep = pull + analyze

```
hotato sweep --stack vapi --since 7d
hotato sweep                                  # the only connected stack
```

Pulls (as above) into `hotato-sweep-<stack>/` (override `--dir`), runs the
same zero-config `analyze`, and writes one self-contained, offline HTML
dashboard (`hotato-sweep-<stack>.html`, override `--out`): the ranked
candidate moments across every call, with a hear-the-bug player on the top
ones. `--format json` emits the candidates plus a pull summary instead.

Dual-channel stacks get separated scoring. Mono/mixed stacks need
`--allow-mono`; without per-party separation their calls land in the
dashboard's Skipped section.

Candidates are MEASURED timing moments you review and label with `hotato
fixture create`: a timestamp and a number, not a verdict on intent.

## What is and isn't pullable

See [`docs/ADAPTER-STATUS.md`](ADAPTER-STATUS.md) for the full map: which
stacks auto-pull dual-channel, which are mono-only, which capture in your
own infra, and which endpoint each uses.

## After the first catch

You have seen a catch on a recorded call. The second move is driving one
against your live agent on demand, fed into this same pull -> score
pipeline: [`docs/DRIVE-A-CALL.md`](DRIVE-A-CALL.md).
