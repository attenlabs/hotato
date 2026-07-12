# Egress: what talks to the network, command by command

Derived from the code, not the marketing copy: every `urllib`/`http`/
`subprocess`-to-a-network-tool call site in `src/hotato/`, mapped to the CLI
command that reaches it. If a command is not listed under "reaches the
network", it does not import `urllib`, does not open a socket, and does not
shell out to a networked tool.

## Fully local -- no network code path exists

`run`, `report`, `doctor`, `team`, `export`, `benchmark`, `compare` (the
batch result-comparison command, `stackbench.py`), `demo`, `start`, `card`,
`diagnose`, `plan`, `explain`, `fixture create`, `fixture promote`,
`contract create` (stereo / caller+agent / local-file mono), `contract
verify`, `contract inspect`, `contract pack`, `contract unpack`, `trace
attach`, `trace export`, `scan`, `trust`, `analyze`, `patch`, `verify`, `fix
trial`, `loop`, `describe`, `init starter`, `init webhook` (the scaffold
generator -- the server it scaffolds is a local listener, see below), `setup`.

These read local files (recordings, scenarios, `hotato.yaml`, connection
files) and write local files. `connect` also belongs here: it validates and
stores credentials at `~/.hotato/connections.json` (mode `0600`) without a
network round trip (`src/hotato/connections.py`: "nothing in this module
makes a network call").

## Reaches your configured vendor -- only when the command's job is to

| Command | Reaches | When | Code |
| --- | --- | --- | --- |
| `capture --stack vapi\|retell\|twilio` | The stack's REST API | Always (fetches the one call you named) | `capture.py`: `_http_get`/`_http_get_json`/`_download` via `urllib.request` |
| `capture --stack vapi\|retell\|twilio --demo` | Nothing | `--demo` scores a bundled reference file instead | `capture.py` |
| `capture --stack livekit\|pipecat` | Nothing from Hotato | The recording is produced by YOUR infra; Hotato only scores the local file `setup` pointed you at | `capture.py` |
| `pull` | The stack's list + download endpoints | Always (bulk-fetches recent recordings) | `capture.py` (same `_http_get`/`_download` path `capture` uses, looped) |
| `sweep --stack <stack>` | Same as `pull` | Whenever `--demo` is NOT passed | `capture.py` via the pull path |
| `sweep --demo` | Nothing | Sweeps the two bundled demo recordings | `analyze.py` on packaged audio |
| `inspect --stack vapi\|retell` | The stack's assistant-config read endpoint (GET only, never a write) | Always | `inspectcfg.py`: `_http_get_json`, `inspect_vapi`, `inspect_retell` |
| `inspect --stack livekit\|pipecat` | Nothing | Parses YOUR local config file with `ast`, no network | `inspectcfg.py`: `inspect_livekit_file`, `inspect_pipecat_file` |
| `ingest` (webhook worker) | Inbound only, from the stack you configured to call your webhook; outbound only if the event's `recording_url` needs a download | Per event, and only for the fetch step | `ingest.py`: `_resolve_recording` reuses `capture.py`'s fetch/download; payloads are always treated as data, never instructions |
| `apply` (default, no `--yes`) | Nothing | Dry run: prints the staging clone it WOULD create | `apply.py`: `build_apply` returns `dry_run: True`, never calls the networked function |
| `apply --clone --yes` | The stack's REST API (`vapi`, `retell` only) | Only with `--yes` and credentials | `apply.py`: `create_clone` / `_http_json` is "the only networked function" in the module (its own docstring) -- reads the source config via GET, then POSTs to create a NEW staging assistant. Never PUTs/PATCHes the source. |
| `issue create` (default, no `--yes`) | Nothing | Dry run: prints the issue body and the `gh` command it would run | `issuecmd.py` |
| `issue create --yes` | GitHub, through your local `gh` CLI's existing auth | Only with `--yes` | `issuecmd.py`: `subprocess.run(["gh", "issue", "create", ...])` |
| `pr create` (default, no `--yes`) | Nothing | Dry run: prints the PR body and the `git`/`gh` argv it would run | `prcmd.py` |
| `pr create --yes` | Git remote + GitHub, through your local `gh` CLI's existing auth | Only with `--yes` | `prcmd.py`: `subprocess.run([...git...])` then `subprocess.run(["gh", "pr", "create", ...])` |
| `test run --state F` (state-config with `adapter: http`) | Your configured system-of-record REST API | Per `state`/`state_change` assertion, and ONLY when the config sets `egress_opt_in: true`. What leaves: the mapped filter VALUES for that one query (in the URL or a JSON body); never audio, transcript, or the config. | `state_adapter.py`: `HttpStateAdapter.query` via `urllib.request` (credential-safe redirects reused from `capture.py`) |
| `test run --state F` (state-config with `adapter: sql` + a `dsn`) | Your database, over the network via the driver you install | Per assertion, and ONLY with `egress_opt_in: true`. A parameterized read-only SELECT; the filter VALUES are bound as data, never interpolated. A local `sqlite_path` opens no socket. | `state_adapter.py`: `SqlStateAdapter.query` (stdlib `sqlite3`, or a caller-installed DBAPI driver) |

Without `egress_opt_in: true`, `load_state_adapter` REFUSES an `http` adapter or
a `sql` adapter over a `dsn` (raises before any connection -- the CLI's exit-2
usage-error path). The default `--state` adapters -- the local mock JSON/SQLite
sandbox and a local `sqlite_path` SQL DB -- open no socket and need no opt-in.
Full config format: [`docs/STATE-ADAPTERS.md`](STATE-ADAPTERS.md).

## Optional extras that add a hosted call

| Extra | Adds | Gate |
| --- | --- | --- |
| `hotato[neural]` | Nothing network -- a local Silero VAD cross-check model, run offline | N/A |
| `hotato[transcribe]` (`run --transcribe`) | Nothing at inference -- a local `faster-whisper` ASR pass over the same recording, fully offline once the chosen model is cached. The first use of a model name not already cached downloads its weights from its public host (a one-time fetch, like installing any pip package with model weights); every run after that opens no socket. Context only -- never fed back into the score. | N/A |
| `hotato[livekit]` / `hotato[pipecat]` | Nothing from Hotato directly -- these SDKs run YOUR live capture infra; Hotato scores the file that infra writes | N/A |
| `--diarizer pyannoteai` (`contract create --mono --diarize`, `run --mono --diarize`) | Uploads the mono audio to `pyannote.ai` for diarization | Refused (exit 2) unless `--egress-opt-in` is passed; the default diarizer (`pyannote`, local) never uploads. See `diarize.py`: `build_pyannoteai_backend`. |
| `--notify URL` (`sweep`, `fleet run`) | POSTs one JSON summary -- counts, top candidate moments (id, kind, timing numbers only), local artifact paths. No audio, no credentials, no transcript text. Plus a `text` line for Slack incoming webhooks. | Off by default; only fires with an explicit, repeatable `--notify URL`. A non-http(s) scheme is refused (exit 2) before any network attempt; once sent, delivery is fail-open -- a down or slow webhook logs one stderr warning and never breaks the run. See `notify.py`: `post_notification`. |

## The one credential-safety detail worth knowing

`capture.py` installs a process-wide `urllib` redirect handler
(`_CredentialSafeRedirectHandler`) so a 3xx redirect from a vendor API can
never carry your Authorization header or API key to a different host. This
applies to every fetch path in the table above that goes through
`capture.py`'s `_http_get`/`_download`.

## Read more

- Posture summary and reporting a vulnerability: [`SECURITY.md`](../SECURITY.md)
- Command-by-command threat model: [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
- Ingest's untrusted-payload handling: [`docs/INGEST.md`](INGEST.md)
