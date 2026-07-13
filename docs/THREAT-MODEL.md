# Threat model

Hotato is built so that the sensitive thing (your call recordings) stays on your
machine, and every network action is one you named. This page is the precise
split: which commands are offline-only, which reach the network and only when you
ask, and what Hotato guarantees it will never do.

## Three guarantees

1. **Hotato never mutates production by default.** No command changes a live
   stack's configuration on its own. `plan` and `patch` produce a proposal;
   `apply` operates on a fresh staging clone and dry-runs by default. There is no
   command that pushes a config to production.
2. **Hotato never uploads your recordings to Attention Labs.** There is no
   hosted backend, no telemetry, and no "phone home." Audio moves only from your
   own stack to your own disk, and only when you run a pull/capture/sweep. The
   one exception is explicit and opt-in: the hosted `--diarizer pyannoteai`
   backend, which requires `--egress-opt-in` before any audio leaves the machine.
3. **Hotato never treats a webhook payload as instructions.** The `ingest`
   webhook worker reads a completed-call notification as **data**: it extracts a
   recording reference and scans it. Payload fields are never executed, shelled
   out, or used to choose what code runs.

## Core: offline, no network, ever

These commands read the local files you point them at and write local files. They
open no sockets. This is the whole scoring, analysis, and fixture surface.

| Command | What it touches |
|---|---|
| `run` | Score a local WAV (or the bundled self-test battery). |
| `scan` | List candidate moments in a local recording. |
| `trust` | Input-health check on a local recording. |
| `report` | Render a self-contained HTML report from local run data. |
| `fixture` | Create / promote a local moment into a regression fixture. |
| `compare` | Score a before/after pair of local takes. |
| `verify` | Roll up before/after run envelopes on disk. |
| `diagnose` | Explain a finished local run envelope (read-only). |
| `plan` | Combine a diagnosis + config into a proposed fix plan JSON. |
| `patch` | Render a fix plan into a paste-ready patch. Never applies it. |
| `demo` | Run the packaged two-call battery. Zero extra files. |
| `team`, `export`, `benchmark` | Aggregate / export local run data. |
| `setup`, `describe`, `init` | Print recording config, emit the CLI manifest, scaffold local integration files. |
| `rubric run`, `rubric calibrate` | Score a local transcript against a rubric with a **LOCAL** model (Ollama at `localhost`). Default path opens no off-box socket; the verdict cache is local. Reaches the network only with an explicit hosted / non-local judge (see below). |
| `test run` (rubric lane) | Same LOCAL model judge as `rubric run`, run inline on a conversation-test's `assertions.rubric` lane. Local by default. |

Default retention is local-only: reports, envelopes, and exports are written
where you point them and nowhere else.

## Network: only when you explicitly request it

These commands reach outside the machine, and only because reaching out is their
job. Each requires you to name a stack, a repository, or a webhook you configured.

| Command | Network surface | Notes |
|---|---|---|
| `connect` | none at connect time | Stores a stack's credentials locally at `0600`. Setup for the pull path; no audio moves. |
| `pull` | your voice stack's API | Bulk-fetch recent recordings from a stack **you** connected, into a local folder. |
| `capture` | your voice stack's API | Fetch and score one real call from your stack. |
| `sweep` | your voice stack's API | `pull` + analyze in one command. |
| `inspect` | your voice stack's API | Read (never write) the current turn-taking config. Read-only. |
| `ingest` | a webhook endpoint you host | Worker that scans each completed call. Payloads are data, not instructions (guarantee 3). |
| `issue` | GitHub, via your local `gh` | File a sweep's candidates as an issue. Uses your existing `gh` auth. |
| `pr` | GitHub, via your local `gh` | Open a PR adding promoted fixtures. Uses your existing `gh` auth. |
| `apply` | a git clone you point it at | Applies a patch to a fresh **staging** clone only, never the source. Dry-run by default; refuses a both-axes threshold funnel. |
| `--diarizer pyannoteai` | Attention Labs hosted diarizer | The only AUDIO path that can send audio off-box, and only with `--egress-opt-in`. The default diarizer is local. |
| `--judge-provider hosted` / non-local `--judge-endpoint` (any rubric command) | a hosted or remote model host you name | Sends the transcript + rubric criterion off-box for judging. Refused (exit 2) unless `--judge-egress-opt-in`. The default judge is a LOCAL Ollama model and never leaves the box. See [`docs/EGRESS.md`](EGRESS.md) and [`docs/RUBRIC.md`](RUBRIC.md). |
| `test run --state` **http adapter** | your system-of-record's REST API | Only when the state-config names `adapter: http`, and only with `egress_opt_in: true` in that config. Sends the mapped filter VALUES for one `state`/`state_change` query; never audio, transcript, or the config itself. See [`docs/STATE-ADAPTERS.md`](STATE-ADAPTERS.md). |
| `test run --state` **sql adapter over a `dsn`** | your database, over the network | Only when the state-config names `adapter: sql` with a `dsn`, and only with `egress_opt_in: true`. A parameterized, read-only SELECT with the mapped filter values bound as data. A local `sqlite_path` opens no socket. |

The state adapters read a post-call **system of record** to ground an Authority-2
`state` assertion (did the refund/appointment get written?). A record
the system of record can be read and does not hold is a grounded FAIL; a system
of record Hotato could **not** reach or read (network error, timeout, 5xx,
non-JSON) is INCONCLUSIVE, never a fabricated verdict. Credentials come from
environment-variable names in the config (never inline secrets), and the HTTP
adapter refuses a plain-`http://` base URL unless `allow_http: true` is set for a
trusted local endpoint. The default `--state` path is the local mock sandbox
(a JSON/SQLite fixture) and a local `sqlite_path` SQL DB, both of which open no
socket and need no opt-in.

Notify surfaces (Slack, GitHub) are used only through credentials you configured
(`gh`, a Slack token) and only for actions you invoked. Hotato ships no default
integrations that fire on their own.

## Network trust: proxies and TLS

Every networked command above (`pull`, `capture`, `sweep`, `inspect`, `apply`,
and the credential probe in `connect`) makes its HTTP calls through Python's
standard `urllib`, which honors the `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
environment-variable convention, the same convention `curl`, `pip`, `git`, and
`docker` follow. That is deliberate: it is what lets Hotato work behind a
corporate proxy with no extra flags. It also means an environment variable set
for the Hotato process controls where its outbound credentialed requests are
routed, and that is a trust decision worth stating plainly instead of leaving
implicit.

Two things bound how far that ambient trust reaches:

- **TLS certificate validation is never disabled.** Every credentialed base
  URL in `capture.py` and `apply.py` is hardcoded `https://` (`api.vapi.ai`,
  `api.retellai.com`, `api.twilio.com`, and the other supported stacks), and
  Hotato never turns off certificate checking. A proxy set via
  `HTTP_PROXY`/`HTTPS_PROXY` can see the `CONNECT` target host and port, and
  can refuse or stall the connection (a denial of service), but it cannot
  read or rewrite the `Authorization` header or the response body without
  also presenting a certificate the machine's own trust store already
  accepts for that vendor's domain, a much larger compromise (a rogue
  trusted root CA already installed) outside `HTTP_PROXY`'s reach and
  outside Hotato's control.
- **The threat prerequisite is already a compromised local environment.** An
  attacker able to set environment variables for the Hotato process can
  already read `VAPI_API_KEY` / `RETELL_API_KEY` / etc. straight out of the
  environment, or read the `0600` credentials file `connect` wrote, both
  strictly easier than standing up a TLS-valid proxy.

If you do not trust the ambient proxy environment a command will run in, set
`HOTATO_NO_PROXY=1` to make Hotato's HTTP opener ignore `HTTP_PROXY` /
`HTTPS_PROXY` for that run, or unset those variables before invoking the
command. The default is unchanged: proxy env vars are honored, matching
curl/pip/git, so a legitimate corporate-proxy setup keeps working with no
configuration.

## What an attacker cannot do through Hotato

- **Cannot exfiltrate recordings by default.** With no `pull`/`capture`/`sweep`
  run and no `--egress-opt-in`, no audio leaves the machine. The default
  diarizer is local.
- **Cannot inject commands via a call webhook.** `ingest` parses a payload for a
  recording reference. It does not evaluate payload fields, and a malicious
  webhook body cannot make Hotato run arbitrary code (guarantee 3).
- **Cannot silently change production.** No command writes to a live stack's
  config. The furthest Hotato goes is a proposed patch you apply to a staging
  clone yourself.

## Verifying the posture

You can confirm the posture directly, without trusting this page:

- **No telemetry.** There is no analytics or "phone home" code path and no
  hosted backend. `pip install hotato` pulls zero runtime dependencies, so there
  is nothing to audit but the standard library.
- **Credentials at `0600`.** `connect` writes stack credentials locally with
  owner-only permissions; check them with `ls -l` on your credentials path.
- **Local-only retention.** Reports, envelopes, and exports exist only where you
  wrote them.
- **Egress is opt-in.** The only off-box audio path is `--diarizer pyannoteai`
  guarded by `--egress-opt-in`; without that flag, no audio leaves the machine.

## Drive-a-call (`run_scenario`): originating a real call

`run_scenario` (`src/hotato/drive.py`, wired into the Vapi and Twilio adapters)
places a REAL outbound call against a live agent and pulls the recording. Its
threat surface and the controls on it:

- **A real, billable call is never placed silently.** `run_scenario` refuses
  unless BOTH real credentials AND an explicit egress opt-in
  (`HOTATO_DRIVE_OPT_IN=1` or `egress_opt_in: true` on the scenario) are present.
  Absent either, it raises a clean structured refusal and dials nothing --
  matching the opt-in posture of `--allow-mono` / `HOTATO_ALLOW_PRIVATE_URLS` /
  `--egress-opt-in`.
- **Production config is never mutated.** The only verbs issued are `POST`
  (create the call) and `GET` (poll status, list the recording, download it) --
  there is no `PUT`/`PATCH`/`DELETE` surface, so a driven call can never alter an
  assistant, a phone number, or any other provider resource in place. For Vapi
  the call is originated FROM the staging clone the experiment created, not the
  production source.
- **The caller side is labelled precisely, never overstated.** The produced
  conversation carries `origin.kind == "real"` with the provider and its call id,
  and `origin.caller` (`scripted-twiml` for the fixed-timeline Twilio caller,
  `assistant-originated` for a Vapi call the assistant placed) -- it never claims
  a human placed the call or that the scripted caller reacted to the agent.
- **The recording download inherits every capture-side control.** The vendor
  URL (`stereoUrl` / `RecordingSid` media) flows through the same validated
  download as `capture`: http(s)-only scheme allowlist, default-deny SSRF
  (loopback/private/metadata refused unless `HOTATO_ALLOW_PRIVATE_URLS=1`),
  cross-host `Authorization`-strip on redirect, and atomic write. The API key is
  never attached to a pre-signed media URL from the vendor's JSON response.

See [`docs/DRIVE-A-CALL.md`](DRIVE-A-CALL.md) and the drive-a-call rows in
[`docs/EGRESS.md`](EGRESS.md).

## Team workspace (`hotato serve`): a new local listening socket

`hotato serve` (`src/hotato/serve/`, wired into the CLI) opens a NEW local HTTP
listening socket to serve the five conversation-QA views over the fleet registry
+ evidence store. Its threat surface and the controls on it:

- **Localhost-default bind.** The server binds `127.0.0.1` unless the operator
  explicitly passes `--host`. A non-loopback bind (e.g. `--host 0.0.0.0`) prints
  a prominent warning at start; it is never the default, and it is never done
  silently.
- **Token auth on every request.** A shared bearer token authenticates every
  request, compared in constant time (`hmac.compare_digest`). It is either
  operator-supplied (`--token` / `--token-file`) or generated with
  `secrets.token_urlsafe` and stored `0600` under the per-workspace state dir.
  Browsers bootstrap an in-memory, HttpOnly session cookie from the printed
  `/?token=…` URL (then the token is stripped from the address bar via a
  redirect); the token itself is never echoed into any response body. An
  unauthenticated request gets `401` and is never routed.
- **Read-only.** The server issues only `SELECT`s against the registry and reads
  evidence blobs by digest; it exposes no write endpoint and mutates no workspace
  data. Reviews and labels stay CLI-driven. The ONLY file it writes is the
  append-only audit log (`…/serve/<workspace>/audit.jsonl`, `0600`), which
  records who (token/session prefix, never the secret), what (method + path, the
  token stripped from the query), when, and the response status of every request.
- **Zero egress.** The server only binds a listening socket; it never opens an
  outbound connection and imports nothing that phones home, so audio, traces, and
  evaluations never leave the machine. A test whitelists loopback and fails if
  any view attempts an external connection.
- **No stored-content execution.** The raw evidence endpoint (`/evidence/<digest>`)
  serves blobs as `text/plain` with `X-Content-Type-Options: nosniff` (and the
  pages set `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`), so a crafted
  evidence blob cannot execute in the viewer. Redacted transcript/trace content is
  scrubbed at the data layer, so it reaches neither the HTML nor the JSON mirror.
- **No path traversal via the workspace id.** The workspace id is used verbatim
  only in parameterized SQL; as a state-directory name it is sanitized so a
  crafted id cannot escape the registry home.

See [`docs/WORKSPACE.md`](WORKSPACE.md).

Security policy and reporting: [SECURITY.md](../SECURITY.md).
