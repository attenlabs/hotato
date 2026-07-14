# Egress: what talks to the network, command by command

Every `urllib`/`http`/`subprocess`-to-a-network-tool call site in
`src/hotato/`, mapped to the CLI command that reaches it, derived straight
from the code. Every command under "Fully local" runs entirely on your
machine — no `urllib` import, no socket, no networked subprocess.

## Fully local — everything runs on your machine

`run`, `report`, `doctor`, `team`, `export`, `benchmark`, `compare` (the
batch result-comparison command, `stackbench.py`), `demo`, `start`, `card`,
`diagnose`, `plan`, `explain`, `fixture create`, `fixture promote`,
`contract create` (stereo / caller+agent / local-file mono), `contract
verify`, `contract inspect`, `contract pack`, `contract unpack`, `trace
attach`, `trace export`, `scan`, `trust`, `analyze`, `patch`, `verify`, `fix
trial`, `loop`, `describe`, `init starter`, `init webhook` (the scaffold
generator — the server it scaffolds is a local listener, see below),
`setup`, and every `counterexample` subcommand (`compile`, `verify`,
`reproduce`, `inspect`, `export`, `predicate`). Counterexample compilation
uses the local scripted simulator and deterministic assertion engine; it never
executes a provider adapter, judge, download, or arbitrary command.

These read local files (recordings, scenarios, `hotato.yaml`, connection
files) and write local files. `connect` also belongs here: it validates and
stores credentials at `~/.hotato/connections.json` (mode `0600`) with no
network round trip (`src/hotato/connections.py`: "nothing in this module
makes a network call").

`rubric run`, `rubric calibrate`, and the `test run` rubric lane also
belong here **on the default path**: the model judge is a **LOCAL Ollama**
daemon (default `http://localhost:11434`), and the verdict cache
(`~/.hotato/rubric-cache`) is local. Two paths do send a rubric command to
the network — both explicit, both in the extras table below: a non-local
Ollama endpoint, or `--judge-provider hosted`.

## Reaches your configured vendor — only when the command's job is to

- **`capture --stack vapi\|retell\|twilio`**
  - Reaches: the stack's REST API.
  - When: always (fetches the one call you named).
  - Code: `capture.py`: `_http_get`/`_http_get_json`/`_download` via
    `urllib.request`.
- **`capture --stack vapi\|retell\|twilio --demo`**
  - Reaches: nothing.
  - When: `--demo` scores a bundled reference file instead.
  - Code: `capture.py`.
- **`capture --stack livekit\|pipecat`**
  - Reaches: nothing from Hotato.
  - When: the recording is produced by YOUR infra; Hotato only scores the
    local file `setup` pointed you at.
  - Code: `capture.py`.
- **`pull`**
  - Reaches: the stack's list + download endpoints.
  - When: always (bulk-fetches recent recordings).
  - Code: `capture.py` (same `_http_get`/`_download` path `capture` uses,
    looped).
- **`sweep --stack <stack>`**
  - Reaches: same as `pull`.
  - When: whenever `--demo` is NOT passed.
  - Code: `capture.py` via the pull path.
- **`sweep --demo`**
  - Reaches: nothing.
  - When: sweeps the two bundled demo recordings.
  - Code: `analyze.py` on packaged audio.
- **`inspect --stack vapi\|retell`**
  - Reaches: the stack's assistant-config read endpoint (GET only, never a
    write).
  - When: always.
  - Code: `inspectcfg.py`: `_http_get_json`, `inspect_vapi`, `inspect_retell`.
- **`inspect --stack livekit\|pipecat`**
  - Reaches: nothing.
  - When: parses YOUR local config file with `ast`, no network.
  - Code: `inspectcfg.py`: `inspect_livekit_file`, `inspect_pipecat_file`.
- **`ingest` (webhook worker)**
  - Reaches: inbound only, from the stack you configured to call your
    webhook; outbound only if the event's `recording_url` needs a download.
  - When: per event, and only for the fetch step.
  - Code: `ingest.py`: `_resolve_recording` reuses `capture.py`'s
    fetch/download; payloads are always treated as data, never instructions.
- **`apply` (default, no `--yes`)**
  - Reaches: nothing.
  - When: dry run — prints the staging clone it WOULD create.
  - Code: `apply.py`: `build_apply` returns `dry_run: True`, never calls the
    networked function.
- **`apply --clone --yes`**
  - Reaches: the stack's REST API (`vapi`, `retell` only).
  - When: only with `--yes` and credentials.
  - Code: `apply.py`: `create_clone` / `_http_json` is "the only networked
    function" in the module (its own docstring) — reads the source config
    via GET, then POSTs to create a NEW staging assistant. Never
    PUTs/PATCHes the source.
- **`issue create` (default, no `--yes`)**
  - Reaches: nothing.
  - When: dry run — prints the issue body and the `gh` command it would
    run.
  - Code: `issuecmd.py`.
- **`issue create --yes`**
  - Reaches: GitHub, through your local `gh` CLI's existing auth.
  - When: only with `--yes`.
  - Code: `issuecmd.py`: `subprocess.run(["gh", "issue", "create", ...])`.
- **`pr create` (default, no `--yes`)**
  - Reaches: nothing.
  - When: dry run — prints the PR body and the `git`/`gh` argv it would
    run.
  - Code: `prcmd.py`.
- **`pr create --yes`**
  - Reaches: Git remote + GitHub, through your local `gh` CLI's existing
    auth.
  - When: only with `--yes`.
  - Code: `prcmd.py`: `subprocess.run([...git...])` then
    `subprocess.run(["gh", "pr", "create", ...])`.
- **`test run --state F`** (state-config with `adapter: http`)
  - Reaches: your configured system-of-record REST API.
  - When: per `state`/`state_change` assertion, and ONLY when the config
    sets `egress_opt_in: true`. What leaves: the mapped filter VALUES for
    that one query (in the URL or a JSON body); never audio, transcript, or
    the config.
  - Code: `state_adapter.py`: `HttpStateAdapter.query` via `urllib.request`
    (credential-safe redirects reused from `capture.py`).
- **`test run --state F`** (state-config with `adapter: sql` + a `dsn`)
  - Reaches: your database, over the network via the driver you install.
  - When: per assertion, and ONLY with `egress_opt_in: true`. A
    parameterized read-only SELECT; the filter VALUES are bound as data,
    never interpolated. A local `sqlite_path` opens no socket.
  - Code: `state_adapter.py`: `SqlStateAdapter.query` (stdlib `sqlite3`, or
    a caller-installed DBAPI driver).

`load_state_adapter` requires `egress_opt_in: true` before an `http`
adapter or a `sql` adapter over a `dsn` connects — that requirement is
checked before any connection, on the CLI's exit-2 usage-error path. The
default `--state` adapters — the local mock JSON/SQLite sandbox and a local
`sqlite_path` SQL DB — run entirely on your machine and need no opt-in.
Full config format: [`docs/STATE-ADAPTERS.md`](STATE-ADAPTERS.md).

## Optional extras that add a hosted call

- **`hotato[neural]`**
  - Adds: nothing network — a local Silero VAD cross-check model, run
    offline.
  - Gate: N/A.
- **`hotato[transcribe]`** (`run --transcribe`)
  - Adds: nothing at inference — a local `faster-whisper` ASR pass over the
    same recording, fully offline once the chosen model is cached. The
    first use of a model name not already cached downloads its weights
    from its public host (a one-time fetch, like installing any pip
    package with model weights); every run after that opens no socket.
    Context only — kept separate from the score.
  - Gate: N/A.
- **`hotato[livekit]` / `hotato[pipecat]`**
  - Adds: nothing from Hotato directly — these SDKs run YOUR live capture
    infra; Hotato scores the file that infra writes.
  - Gate: N/A.
- **`--diarizer pyannoteai`** (`contract create --mono --diarize`,
  `run --mono --diarize`)
  - Adds: uploads the mono audio to `pyannote.ai` for diarization.
  - Gate: requires `--egress-opt-in` (exit 2 without it); the default
    diarizer (`pyannote`, local) stays offline. See `diarize.py`:
    `build_pyannoteai_backend`.
- **`hotato[judge]`** (`rubric run`, `rubric calibrate`, `test run` rubric
  lane)
  - Adds: nothing on the default path — the judge is a LOCAL Ollama model
    (`http://localhost:11434`), reached with only the stdlib (`urllib`).
    The transcript stays on your machine.
  - Gate: N/A on the default (local) path. `rubric.py`: `OllamaJudge`.
- **`--judge-provider hosted --judge-endpoint URL`** (any rubric command)
  - Adds: sends the transcript + rubric criterion to a hosted
    OpenAI/Anthropic-compatible `/chat/completions` endpoint you name.
  - Gate: requires `--judge-egress-opt-in` (exit 2 without it); the default
    local judge stays on your machine. `rubric.py`: `HostedJudge` (raises
    `EgressRefused` without the flag).
- **A non-local `--judge-endpoint`** (e.g. a remote Ollama host)
  - Adds: reaches that host.
  - Gate: same gate — requires `--judge-egress-opt-in`.
    `localhost`/`127.0.0.1`/`::1` skip it. `rubric.py`:
    `OllamaJudge._is_local_endpoint`.
- **`--notify URL`** (`sweep`, `fleet run`)
  - Adds: POSTs one JSON summary — counts, top candidate moments (id,
    kind, timing numbers only), local artifact paths. Audio, credentials,
    and transcript text stay out of it. Plus a `text` line for Slack
    incoming webhooks.
  - Gate: off by default; only fires with an explicit, repeatable `--notify
    URL`. A non-http(s) scheme is refused (exit 2) before any network
    attempt; once sent, delivery is fail-open — a down or slow webhook
    logs one stderr warning and the run keeps going. See `notify.py`:
    `post_notification`.

## The one credential-safety detail worth knowing

`capture.py` installs a process-wide `urllib` redirect handler
(`_CredentialSafeRedirectHandler`) so a 3xx redirect from a vendor API keeps
your Authorization header and API key scoped to the original host. This
applies to every fetch path in the table above that goes through
`capture.py`'s `_http_get`/`_download`.

## Read more

- Posture summary and reporting a vulnerability: [`SECURITY.md`](../SECURITY.md)
- Command-by-command threat model: [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
- Ingest's untrusted-payload handling: [`docs/INGEST.md`](INGEST.md)

## Drive-a-call — originates a real call, so it is double-gated

`run_scenario` (the fleet experiment step; `src/hotato/drive.py`)
ORIGINATES a real, billable call against a live agent and pulls its
recording. It is the one path here that both reaches a vendor AND places an
outbound call, so it carries a gate on top of the usual credential
requirement.

- **`run_scenario` (Vapi adapter)**
  - Reaches: `POST https://api.vapi.ai/call`, then polls `GET /call/{id}`,
    then the existing `capture_vapi` download.
  - When: only when driven.
  - Gate: requires real credentials AND an explicit egress opt-in
    (`HOTATO_DRIVE_OPT_IN=1` or `egress_opt_in: true` on the scenario) both
    present before dialing. Only creates and reads (GET/POST); the call is
    driven from the staging CLONE, keeping production untouched.
- **`run_scenario` (Twilio adapter)**
  - Reaches: `POST .../Calls.json`, then polls `GET .../Calls/{sid}.json` +
    `GET .../Recordings.json`, then the existing `capture_twilio` download.
  - When: only when driven.
  - Gate: same double gate. TwiML `<Say>` is a fixed-timeline scripted
    caller; recording is dual-channel via
    `Record=true`/`RecordingChannels=dual`. GET/POST only, so config stays
    untouched.

The recording download reuses `capture.py`'s validated fetch (scheme
allowlist, default-deny SSRF, cross-host credential strip, atomic write); a
local test recording server on `127.0.0.1` needs
`HOTATO_ALLOW_PRIVATE_URLS=1` like every other download. Full walkthrough:
[`docs/DRIVE-A-CALL.md`](DRIVE-A-CALL.md).
