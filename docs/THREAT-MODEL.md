# Threat model

The sensitive thing here -- your call recordings -- stays on your machine,
and every network action is one you named. This page draws the precise
line: which commands are offline-only, which reach the network and only
when you ask, and the guarantees that hold either way.

## Three guarantees

1. **Every production change happens through a proposal you apply
   yourself.** `plan` and `patch` produce a proposal; `apply` operates on a
   fresh staging clone and dry-runs by default. Pushing a config to
   production is a step you take, not one any command takes on its own.
2. **Recordings stay on your machine.** Self-hosted, with audio moving only
   from your own stack to your own disk, and only when you run a
   pull/capture/sweep. The one exception is explicit and opt-in: the hosted
   `--diarizer pyannoteai` backend, which requires `--egress-opt-in` before
   any audio leaves the machine.
3. **A webhook payload is read strictly as data.** The `ingest` webhook
   worker reads a completed-call notification as **data**: it extracts a
   recording reference and scans it. Payload fields are read, never
   executed, shelled out, or used to choose what code runs.

## Core: fully offline, on your machine

These commands read and write only the local files you point them at,
opening no sockets. This is the whole scoring, analysis, and fixture
surface.

- **`run`** -- score a local WAV (or the bundled self-test battery).
- **`scan`** -- list candidate moments in a local recording.
- **`trust`** -- input-health check on a local recording.
- **`report`** -- render a self-contained HTML report from local run data.
- **`fixture`** -- create / promote a local moment into a regression
  fixture.
- **`compare`** -- score a before/after pair of local takes.
- **`verify`** -- roll up before/after run envelopes on disk.
- **`diagnose`** -- explain a finished local run envelope (read-only).
- **`plan`** -- combine a diagnosis + config into a proposed fix plan
  JSON.
- **`patch`** -- render a fix plan into a paste-ready patch. Never applies
  it.
- **`demo`** -- run the packaged two-call battery. Zero extra files.
- **`team`, `export`, `benchmark`** -- aggregate / export local run data.
- **`setup`, `describe`, `init`** -- print recording config, emit the CLI
  manifest, scaffold local integration files.
- **`rubric run`, `rubric calibrate`** -- score a local transcript against
  a rubric with a **local** model (Ollama at `localhost`). Default path
  opens no off-box socket; the verdict cache is local. Reaches the network
  only with an explicit hosted / non-local judge (see below).
- **`test run` (rubric lane)** -- same local model judge as `rubric run`,
  run inline on a conversation-test's `assertions.rubric` lane. Local by
  default.
- **`counterexample compile|verify|reproduce|inspect|export|predicate`** --
  read local scenario/test/capsule files and write a new local capsule or
  share-safe projection. The proof path loads no provider adapter, model,
  subprocess, or network client.

Default retention is local-only: reports, envelopes, and exports land
exactly where you point them.

### Counterexample capsule boundary

A private `.hotato-repro` contains the source scenario and test, reduced
fixture, target assertion result, and proof journal. Treat it as sensitive
source material. The compiler creates it with owner-only modes where the host
supports POSIX permissions, never overwrites an existing destination, and
promotes a sibling staging directory only after all files and hashes exist.

The verifier rejects path traversal, symlinked members and directories,
special files, undeclared files, oversized/deep inputs, digest mismatches,
broken deletion chains, evaluator drift on the strict proof path, and a
minimality claim that admits a preserving unit deletion. `reproduce` is a
separate current-evaluator check: it permits evaluator drift, but still
validates capsule integrity and the source-to-final delete-only chain.

`counterexample export` derives a non-runnable projection after strict private
verification. It omits scenario/test bodies, transcript content, tool payloads,
state values, and provider identifiers. Its hashes are correlators, so a team
with low-entropy or externally known inputs should keep even the projection in
its normal engineering-artifact access boundary.

## Network: only when you explicitly request it

These commands reach outside the machine, and only because reaching out is
their job. Each requires you to name a stack, a repository, or a webhook
you configured.

- **`connect`**
  - Network surface: none at connect time.
  - Notes: stores a stack's credentials locally at `0600`. Setup for the
    pull path; no audio moves.
- **`pull`**
  - Network surface: your voice stack's API.
  - Notes: bulk-fetch recent recordings from a stack **you** connected,
    into a local folder.
- **`capture`**
  - Network surface: your voice stack's API.
  - Notes: fetch and score one call from your stack.
- **`sweep`**
  - Network surface: your voice stack's API.
  - Notes: `pull` + analyze in one command.
- **`inspect`**
  - Network surface: your voice stack's API.
  - Notes: read (never write) the current turn-taking config. Read-only.
- **`ingest`**
  - Network surface: a webhook endpoint you host.
  - Notes: worker that scans each completed call. Payloads are data, not
    instructions (guarantee 3).
- **`issue`**
  - Network surface: GitHub, via your local `gh`.
  - Notes: file a sweep's candidates as an issue. Uses your existing `gh`
    auth.
- **`pr`**
  - Network surface: GitHub, via your local `gh`.
  - Notes: open a PR adding promoted fixtures. Uses your existing `gh`
    auth.
- **`apply`**
  - Network surface: a git clone you point it at.
  - Notes: applies a patch to a fresh **staging** clone only, never the
    source. Dry-run by default; refuses a both-axes threshold funnel.
- **`--diarizer pyannoteai`**
  - Network surface: Attention Labs hosted diarizer.
  - Notes: the only audio path that can send audio off-box, and only with
    `--egress-opt-in`. The default diarizer is local.
- **`--judge-provider hosted` / non-local `--judge-endpoint`** (any rubric
  command)
  - Network surface: a hosted or remote model host you name.
  - Notes: sends the transcript + rubric criterion off-box for judging.
    Refused (exit 2) unless `--judge-egress-opt-in`. The default judge is a
    local Ollama model that stays on the box. See
    [`docs/EGRESS.md`](EGRESS.md) and [`docs/RUBRIC.md`](RUBRIC.md).
- **`test run --state` http adapter**
  - Network surface: your system-of-record's REST API.
  - Notes: only when the state-config names `adapter: http`, and only with
    `egress_opt_in: true` in that config. Sends the mapped filter VALUES for
    one `state`/`state_change` query -- audio, transcript, and the config
    itself stay local. See [`docs/STATE-ADAPTERS.md`](STATE-ADAPTERS.md).
- **`test run --state` sql adapter over a `dsn`**
  - Network surface: your database, over the network.
  - Notes: only when the state-config names `adapter: sql` with a `dsn`,
    and only with `egress_opt_in: true`. A parameterized, read-only SELECT
    with the mapped filter values bound as data. A local `sqlite_path`
    opens no socket.

The state adapters read a post-call **system of record** to ground an
Authority-2 `state` assertion (did the refund/appointment get written?). A
record the system of record can be read and does not hold is a grounded
FAIL; a system of record Hotato could **not** reach or read (network error,
timeout, 5xx, non-JSON) is INCONCLUSIVE, never a guessed verdict.
Credentials come from environment-variable **names** in the config, keeping
the secret itself out of it, and the HTTP adapter refuses a plain-`http://`
base URL unless `allow_http: true` is set for a trusted local endpoint. The
default `--state` path is the local mock sandbox (a JSON/SQLite fixture)
and a local `sqlite_path` SQL DB, both running entirely on-machine, with no
opt-in needed.

Notify surfaces (Slack, GitHub) fire only through credentials you
configured (`gh`, a Slack token) and only for actions you invoked -- every
integration is one you triggered.

## Network trust: proxies and TLS

Every networked command above (`pull`, `capture`, `sweep`, `inspect`,
`apply`, and the credential probe in `connect`) makes its HTTP calls through
Python's standard `urllib`, which honors the `HTTP_PROXY` / `HTTPS_PROXY` /
`NO_PROXY` environment-variable convention, the same convention `curl`,
`pip`, `git`, and `docker` follow. That is deliberate: it is what lets
Hotato work behind a corporate proxy with no extra flags. It also means an
environment variable set for the Hotato process controls where its outbound
credentialed requests are routed, and that is a trust decision worth
stating plainly instead of leaving implicit.

Two things bound how far that ambient trust reaches:

- **TLS certificate validation is always enforced.** Every credentialed
  base URL in `capture.py` and `apply.py` is hardcoded `https://`
  (`api.vapi.ai`, `api.retellai.com`, `api.twilio.com`, and the other
  supported stacks). A proxy set via `HTTP_PROXY`/`HTTPS_PROXY` can see the
  `CONNECT` target host and port, and can refuse or stall the connection (a
  denial of service), but reading or rewriting the `Authorization` header
  or the response body requires also presenting a certificate the
  machine's own trust store already accepts for that vendor's domain -- a
  much larger compromise (a rogue trusted root CA already installed)
  outside `HTTP_PROXY`'s reach and outside Hotato's control.
- **The threat prerequisite is already a compromised local environment.**
  An attacker able to set environment variables for the Hotato process can
  already read `VAPI_API_KEY` / `RETELL_API_KEY` / etc. straight out of the
  environment, or read the `0600` credentials file `connect` wrote -- both
  strictly easier than standing up a TLS-valid proxy.

If you do not trust the ambient proxy environment a command will run in,
set `HOTATO_NO_PROXY=1` to make Hotato's HTTP opener ignore `HTTP_PROXY` /
`HTTPS_PROXY` for that run, or unset those variables before invoking the
command. The default is unchanged: proxy env vars are honored, matching
curl/pip/git, so a corporate-proxy setup keeps working with no
configuration.

## Hotato's containment guarantees

- **Recordings stay on the machine by default.** Audio leaves only when you
  run `pull`/`capture`/`sweep` with `--egress-opt-in`; the default diarizer
  is local.
- **A call webhook is read as data, never as commands.** `ingest` parses a
  payload for a recording reference and stops there: payload fields are
  read, never evaluated, so a malicious webhook body cannot make Hotato run
  arbitrary code (guarantee 3).
- **Production changes always go through you.** No command writes to a live
  stack's config directly; the furthest Hotato goes is a proposed patch you
  apply to a staging clone yourself.

## Verifying the posture

Confirm the posture directly, without trusting this page:

- **Self-hosted, nothing to audit but the standard library.** `pip install
  hotato` pulls zero runtime dependencies; the only network code path is
  the one you explicitly invoke.
- **Credentials at `0600`.** `connect` writes stack credentials locally
  with owner-only permissions; check them with `ls -l` on your credentials
  path.
- **Local-only retention.** Reports, envelopes, and exports exist exactly
  where you wrote them.
- **Egress is opt-in.** The only off-box audio path is `--diarizer
  pyannoteai`, gated by `--egress-opt-in` -- audio stays on the machine
  until you set it.

## Drive-a-call (`run_scenario`): placing a live call

`run_scenario` (`src/hotato/drive.py`, wired into the Vapi and Twilio
adapters) places an outbound call against a live agent and pulls the
recording. Its threat surface and the controls on it:

- **A billable, outbound call always requires explicit opt-in.**
  `run_scenario` places the call only when BOTH live credentials AND an
  explicit egress opt-in (`HOTATO_DRIVE_OPT_IN=1` or `egress_opt_in: true`
  on the scenario) are present. Absent either, it raises a clean
  structured refusal and dials nothing -- matching the opt-in posture of
  `--allow-mono` / `HOTATO_ALLOW_PRIVATE_URLS` / `--egress-opt-in`.
- **Production config stays untouched.** The surface is create-and-read
  only: the only verbs issued are `POST` (create the call) and `GET` (poll
  status, list the recording, download it), with no `PUT`/`PATCH`/`DELETE`
  path to alter an assistant, a phone number, or any other provider
  resource in place. For Vapi the call originates FROM the staging clone
  the experiment created, keeping the production source untouched.
- **The caller side is labelled precisely.** The produced conversation
  carries `origin.kind == "real"` with the provider and its call id, and
  `origin.caller` (`scripted-twiml` for the fixed-timeline Twilio caller,
  `assistant-originated` for a Vapi call the assistant placed), stating
  exactly what happened -- no claim that a human placed the call or that
  the scripted caller reacted to the agent.
- **The recording download inherits every capture-side control.** The
  vendor URL (`stereoUrl` / `RecordingSid` media) flows through the same
  validated download as `capture`: http(s)-only scheme allowlist,
  default-deny SSRF (loopback/private/metadata refused unless
  `HOTATO_ALLOW_PRIVATE_URLS=1`), cross-host `Authorization`-strip on
  redirect, and atomic write. The API key is never attached to a
  pre-signed media URL from the vendor's JSON response.

See [`docs/DRIVE-A-CALL.md`](DRIVE-A-CALL.md) and the drive-a-call rows in
[`docs/EGRESS.md`](EGRESS.md).

## Team workspace (`hotato serve`): a new local listening socket

`hotato serve` (`src/hotato/serve/`, wired into the CLI) opens a new local
HTTP listening socket to serve the five conversation-QA views over the
fleet registry + evidence store. Its threat surface and the controls on
it:

- **Localhost-default bind.** The server binds `127.0.0.1` unless the
  operator explicitly passes `--host`. A non-loopback bind (e.g. `--host
  0.0.0.0`) always prints a prominent warning at start and always requires
  that explicit flag.
- **Token auth on every request.** A shared bearer token authenticates
  every request, compared in constant time (`hmac.compare_digest`). It is
  either operator-supplied (`--token` / `--token-file`) or generated with
  `secrets.token_urlsafe` and stored `0600` under the per-workspace state
  dir. Browsers bootstrap an in-memory, HttpOnly session cookie from the
  printed `/?token=…` URL (then the token is stripped from the address bar
  via a redirect); the token stays out of every response body. An
  unauthenticated request gets `401` before it is routed anywhere.
- **Read-only.** The server issues only `SELECT`s against the registry and
  reads evidence blobs by digest, exposing only reads: no write endpoint,
  no workspace mutation. Reviews and labels stay CLI-driven. The only file
  it writes is the append-only audit log (`…/serve/<workspace>/audit.jsonl`,
  `0600`), which records who (token/session prefix, the secret stays out of
  it), what (method + path, the token stripped from the query), when, and
  the response status of every request.
- **Zero egress.** The server only binds a listening socket, opening no
  outbound connection and importing nothing that phones home -- audio,
  traces, and evaluations stay on the machine. A test whitelists loopback
  and fails if any view attempts an external connection.
- **Evidence renders as data, never as executable content.** The raw
  evidence endpoint (`/evidence/<digest>`) serves blobs as `text/plain`
  with `X-Content-Type-Options: nosniff` (and the pages set
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`), keeping a
  crafted evidence blob inert in the viewer. Redacted transcript/trace
  content is scrubbed at the data layer, so both the HTML and the JSON
  mirror render only the `[redacted]` placeholder.
- **Workspace id sanitized against path traversal.** The workspace id is
  used verbatim only in parameterized SQL; as a state-directory name it is
  sanitized to stay inside the registry home.

See [`docs/WORKSPACE.md`](WORKSPACE.md).

Security policy and reporting: [SECURITY.md](../SECURITY.md).
