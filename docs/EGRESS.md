# Egress: what talks to the network, command by command

Every `urllib`/`http`/`subprocess`-to-a-network-tool call site in
`src/hotato/`, mapped to the CLI command that reaches it -- derived straight
from the code. Every command under "Fully local" runs entirely on your
machine: no `urllib` import, no socket, no networked subprocess.

## Fully local -- everything runs on your machine

`run`, `report`, `doctor`, `team`, `export`, `benchmark`, `compare` (the
batch result-comparison command, `stackbench.py`), `demo`, `start`, `card`,
`diagnose`, `plan`, `explain`, `fixture create`, `fixture promote`,
`contract create` (stereo / caller+agent / local-file mono), `contract
verify`, `contract inspect`, `contract pack`, `contract unpack`, `trace
attach`, `trace export`, `scan`, `trust`, `analyze`, `patch`, `verify`, `fix
trial`, `loop`, `describe`, `init starter`, `init webhook` (the scaffold
generator -- the server it scaffolds is a local listener, see below),
`setup`, and every `counterexample` subcommand (`compile`, `verify`,
`reproduce`, `inspect`, `export`, `predicate`). Counterexample compilation
uses the local scripted simulator and deterministic assertion engine; it never
executes a provider adapter, judge, download, or arbitrary command.

These read and write only local files (recordings, scenarios, `hotato.yaml`,
connection files). `connect` also belongs here: it validates and stores
credentials at `~/.hotato/connections.json` (mode `0600`), no network round
trip (`src/hotato/connections.py`: "nothing in this module makes a network
call").

`rubric run`, `rubric calibrate`, and the `test run` rubric lane also belong
here **on the default path**: the judge is a **LOCAL Ollama** daemon
(default `http://localhost:11434`), and the verdict cache
(`~/.hotato/rubric-cache`) is local too. Two paths send a rubric command to
the network instead -- both explicit, both in the extras table below: a
non-local Ollama endpoint, or `--judge-provider hosted`.

## Reaches your configured vendor -- only when the command's job is to

| Command | Reaches | When | Code |
|---|---|---|---|
| `capture --stack vapi\|retell\|twilio` | the stack's REST API | always (fetches the one call you named) | `capture.py`: `_http_get`/`_http_get_json`/`_download` via `urllib.request` |
| `capture --stack vapi\|retell\|twilio --demo` | nothing | `--demo` scores a bundled reference file instead | `capture.py` |
| `capture --stack livekit\|pipecat` | nothing from Hotato | the recording is produced by YOUR infra; Hotato only scores the local file `setup` pointed you at | `capture.py` |
| `pull` | the stack's list + download endpoints | always (bulk-fetches recent recordings) | `capture.py` (same `_http_get`/`_download` path `capture` uses, looped) |
| `sweep --stack <stack>` | same as `pull` | whenever `--demo` is NOT passed | `capture.py` via the pull path |
| `sweep --demo` | nothing | sweeps the two bundled demo recordings | `analyze.py` on packaged audio |
| `inspect --stack vapi\|retell` | the stack's assistant-config read endpoint (GET only, never a write) | always | `inspectcfg.py`: `_http_get_json`, `inspect_vapi`, `inspect_retell` |
| `inspect --stack livekit\|pipecat` | nothing | parses YOUR local config file with `ast`, no network | `inspectcfg.py`: `inspect_livekit_file`, `inspect_pipecat_file` |
| `ingest` (webhook worker) | inbound only, from the stack you configured to call your webhook; outbound only if the event's `recording_url` needs a download | per event, and only for the fetch step | `ingest.py`: `_resolve_recording` reuses `capture.py`'s fetch/download; payloads are always treated as data, never instructions |
| `apply` (default, no `--yes`) | nothing | dry run -- prints the staging clone it WOULD create | `apply.py`: `build_apply` returns `dry_run: True`, never calls the networked function |
| `apply --clone --yes` | the stack's REST API (`vapi`, `retell` only) | only with `--yes` and credentials | `apply.py`: `create_clone` / `_http_json` is "the only networked function" in the module (its own docstring) -- reads the source config via GET, then POSTs to create a NEW staging assistant. Never PUTs/PATCHes the source. |
| `issue create` (default, no `--yes`) | nothing | dry run -- prints the issue body and the `gh` command it would run | `issuecmd.py` |
| `issue create --yes` | GitHub, through your local `gh` CLI's existing auth | only with `--yes` | `issuecmd.py`: `subprocess.run(["gh", "issue", "create", ...])` |
| `pr create` (default, no `--yes`) | nothing | dry run -- prints the PR body and the `git`/`gh` argv it would run | `prcmd.py` |
| `pr create --yes` | Git remote + GitHub, through your local `gh` CLI's existing auth | only with `--yes` | `prcmd.py`: `subprocess.run([...git...])` then `subprocess.run(["gh", "pr", "create", ...])` |
| `test run --state F` (state-config `adapter: http`) | your configured system-of-record REST API | per `state`/`state_change` assertion, and ONLY when the config sets `egress_opt_in: true`. What leaves: the mapped filter VALUES for that one query (in the URL or a JSON body); never audio, transcript, or the config | `state_adapter.py`: `HttpStateAdapter.query` via `urllib.request` (credential-safe redirects reused from `capture.py`) |
| `test run --state F` (state-config `adapter: sql` + a `dsn`) | your database, over the network via the driver you install | per assertion, and ONLY with `egress_opt_in: true`. A parameterized read-only SELECT; filter VALUES are bound as data, never interpolated. A local `sqlite_path` opens no socket | `state_adapter.py`: `SqlStateAdapter.query` (stdlib `sqlite3`, or a caller-installed DBAPI driver) |

`load_state_adapter` requires `egress_opt_in: true` before an `http`
adapter or a `sql` adapter over a `dsn` connects -- checked before any
connection, on the CLI's exit-2 usage-error path. The default `--state`
adapters (the local mock JSON/SQLite sandbox and a local `sqlite_path` SQL
DB) run entirely on your machine and need no opt-in. Full config format:
[`docs/STATE-ADAPTERS.md`](STATE-ADAPTERS.md).

Notify surfaces (Slack, GitHub) fire only through credentials you
configured (`gh`, a Slack token), and only for actions you invoked.

## Optional extras that add a hosted call

| Extra / flag | Adds | Gate |
|---|---|---|
| `hotato[neural]` | nothing network -- a local Silero VAD cross-check model, run offline | N/A |
| `hotato[transcribe]` (`run --transcribe`) | nothing at inference -- a local `faster-whisper` ASR pass over the same recording, fully offline once the model is cached. First use of an uncached model name downloads its weights once (like installing any pip package with model weights); every run after opens no socket. Context only, kept separate from the score | N/A |
| `hotato[livekit]` / `hotato[pipecat]` | nothing from Hotato directly -- these SDKs run YOUR live capture infra; Hotato scores the file that infra writes | N/A |
| `--diarizer pyannoteai` (`contract create --mono --diarize`, `run --mono --diarize`) | uploads the mono audio to `pyannote.ai` for diarization | requires `--egress-opt-in` (exit 2 without it); the default diarizer (`pyannote`, local) stays offline. See `diarize.py`: `build_pyannoteai_backend` |
| `hotato[judge]` (`rubric run`, `rubric calibrate`, `test run` rubric lane) | nothing on the default path -- the judge is a LOCAL Ollama model (`http://localhost:11434`), reached with only the stdlib (`urllib`). The transcript stays on your machine | N/A on the default (local) path. `rubric.py`: `OllamaJudge` |
| `--judge-provider hosted --judge-endpoint URL` (any rubric command) | sends the transcript + rubric criterion to a hosted OpenAI/Anthropic-compatible `/chat/completions` endpoint you name | requires `--judge-egress-opt-in` (exit 2 without it); the default local judge stays on your machine. `rubric.py`: `HostedJudge` (raises `EgressRefused` without the flag) |
| A non-local `--judge-endpoint` (e.g. a remote Ollama host) | reaches that host | same gate -- requires `--judge-egress-opt-in`. `localhost`/`127.0.0.1`/`::1` skip it. `rubric.py`: `OllamaJudge._is_local_endpoint` |
| `--notify URL` (`sweep`, `fleet run`) | POSTs one JSON summary -- counts, top candidate moments (id, kind, timing numbers only), local artifact paths, plus a `text` line for Slack incoming webhooks. Audio, credentials, and transcript text stay out of it | off by default; fires only with an explicit, repeatable `--notify URL`. A non-http(s) scheme is refused (exit 2) before any network attempt. Once sent, delivery is fail-open -- a down or slow webhook logs one stderr warning and the run keeps going. See `notify.py`: `post_notification` |
| `--notify URL` (`contract verify`) | POSTs one JSON run-summary when the gate finishes -- kind `contract-verify`, the pass/fail counts (`passed`/`failed`/`tampered`/`refused`/`assertions_failed`, reported as separate fields, never blended), the top FAILING contracts' ids + measured timing (`did_yield`/`seconds_to_yield`/`talk_over_sec`), plus a `text` line for Slack. Audio, credentials, transcript text, and file/bundle paths all stay out of it (this payload carries no artifact paths, unlike sweep's) | off by default; fires on every verify (pass or fail) only with an explicit, repeatable `--notify URL`. A non-http(s) scheme is refused (exit 2) before the re-score. Delivery is fail-open -- a down webhook never raises and never changes the verify exit code. See `notify.py`: `contract_verify_payload` / `post_notification` |

## The one credential-safety detail worth knowing

`capture.py` installs a process-wide `urllib` redirect handler
(`_CredentialSafeRedirectHandler`) so a 3xx redirect from a vendor API keeps
your Authorization header and API key scoped to the original host. Applies
to every fetch path above that goes through `capture.py`'s
`_http_get`/`_download`.

## Read more

- Posture summary and reporting a vulnerability: [`SECURITY.md`](../SECURITY.md)
- Command-by-command threat model: [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
- Ingest's untrusted-payload handling: [`docs/INGEST.md`](INGEST.md)

## Drive-a-call: originates a real call, so it is double-gated

`run_scenario` (the fleet experiment step; `src/hotato/drive.py`)
ORIGINATES a real, billable call against a live agent and pulls its
recording -- the one path here that both reaches a vendor AND places an
outbound call, so it carries a gate on top of the usual credential
requirement.

- **`run_scenario` (Vapi adapter)**
  - Reaches: `POST https://api.vapi.ai/call`, then polls `GET /call/{id}`,
    then the existing `capture_vapi` download.
  - When: only when driven.
  - Gate: requires real credentials AND an explicit egress opt-in
    (`HOTATO_DRIVE_OPT_IN=1` or `egress_opt_in: true` on the scenario),
    both present before dialing. Creates and reads only (GET/POST); the
    call is driven from the staging CLONE, keeping production untouched.
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
