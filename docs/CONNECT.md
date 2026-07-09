# Connect, pull, sweep: score every real call across your stack

`hotato sweep` is the "connect once, see every turn-taking problem across all
your real calls" flow. It has three steps you can also run on their own:

1. **`hotato connect <stack>`**: store a stack's credentials once, locally.
2. **`hotato pull`**: bulk-fetch your recent recordings into a folder.
3. **`hotato sweep`**: pull, then run the zero-config `analyze` over them and
   write one offline dashboard of the ranked turn-taking moments.

Everything scores offline. The only network is the direct recording download
from your vendor to your machine; your audio and your keys are never sent to
Hotato.

## 1. Connect once

```
hotato connect vapi --api-key <key>
```

This does a lightweight live auth check (it lists one recent call), then writes
the credentials to `~/.hotato/connections.json` with file mode `0600` (directory
`0700`). The key is never printed and never leaves your machine except to the
vendor's own API. Credentials also fall back to the stack's environment variable,
so this works too:

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

Retell has no list endpoint to verify against, so `connect retell` stores the
key and validates it on the first pull. Use `--no-verify` to skip the live check
for any stack. LiveKit and Pipecat are capture-in-your-infra. There is no
vendor recording to pull, so they are not connectable; use `hotato setup
--stack livekit|pipecat` instead.

After connecting, `--stack` and the credential flags are optional for `pull` and
`sweep`: when exactly one stack is connected, Hotato uses it.

## 2. Pull recent recordings

```
hotato pull --stack vapi --since 7d --limit 50
hotato pull                                   # the only connected stack
```

`pull` lists your recent recordings with the vendor's verified list endpoint and
downloads each one by looping the same single-call fetch `hotato capture` uses,
into `hotato-pull-<stack>/` (override with `--out DIR`). `--since` accepts `7d`,
`12h`, `30m`, `2w`. A recording that cannot be fetched (missing URL, HTTP error,
wrong channel count) is reported as a clean skip with its reason and the pull
continues. One bad call never crashes the run.

**Retell has no verified list endpoint.** Pull it from explicit ids instead
(Hotato never fabricates an endpoint):

```
hotato pull --stack retell --call-id c1 --call-id c2
```

For Twilio, `--call-id` values are Recording SIDs (`RE...`).

**Mono / mixed stacks** (bland, elevenlabs, synthflow, millis, cartesia) produce
a single combined recording with no per-party channel, so talk-over cannot be
attributed. They require `--allow-mono` and are indicative only:

```
hotato pull --stack bland --allow-mono --limit 20
```

## 3. Sweep = pull + analyze

```
hotato sweep --stack vapi --since 7d
hotato sweep                                  # the only connected stack
```

`sweep` pulls (as above) into `hotato-sweep-<stack>/` (override with `--dir`),
then runs the exact same zero-config `analyze` over the folder and writes one
self-contained, offline HTML dashboard (`hotato-sweep-<stack>.html`, override
with `--out`) of the ranked candidate turn-taking moments across every call,
with the hear-the-bug audio player on the top moments. `--format json` emits the
ranked candidates plus a pull summary instead.

Dual-channel stacks give separated scoring. Mono/mixed stacks can be swept with
`--allow-mono`, but their calls cannot be attributed per party and surface in the
dashboard's Skipped section.

Candidates are MEASURED timing moments you review and label with `hotato fixture
create`, never verdicts and never intent. There is no pass/fail, no failure
count, and no accuracy number anywhere.

## What is and isn't pullable

See [`docs/ADAPTER-STATUS.md`](ADAPTER-STATUS.md) for the full map:
which stacks auto-pull dual-channel, which are mono-only behind `--allow-mono`,
which are capture-in-your-infra, and which are not integrable, each with the
exact verified endpoint and the gaps.
