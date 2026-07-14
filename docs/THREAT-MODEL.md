# Threat model

Your call recordings stay on your machine. Every network action is one you
name. This page draws the line: which commands are offline-only, which
reach the network, and what holds true either way.

## Three guarantees

1. **You apply every production change yourself.** `plan` and `patch`
   produce a proposal; `apply` dry-runs by default on a fresh staging
   clone. No command pushes to production on its own.
2. **Recordings stay on your machine.** Audio moves only from your stack to
   your disk, only when you run `pull`/`capture`/`sweep`. The one opt-in
   exception: the hosted `--diarizer pyannoteai` backend, gated by
   `--egress-opt-in`.
3. **A webhook payload is data, never code.** `ingest` reads a
   completed-call notification, extracts a recording reference, and scans
   it. Payload fields are read, never executed or used to choose what
   runs.

## Core: fully offline, on your machine

These commands read and write only the local files you point them at. They
open no sockets -- this is the whole scoring, analysis, and fixture
surface.

| Command | What it does |
|---|---|
| `run` | score a local WAV (or the bundled self-test battery) |
| `scan` | list candidate moments in a local recording |
| `trust` | input-health check on a local recording |
| `report` | render a self-contained HTML report from local run data |
| `fixture` | create or promote a local moment into a regression fixture |
| `compare` | score a before/after pair of local takes |
| `verify` | roll up before/after run envelopes on disk |
| `diagnose` | explain a finished local run envelope (read-only) |
| `plan` | combine a diagnosis and config into a fix-plan JSON |
| `patch` | render a fix plan into a paste-ready patch; never applies it |
| `demo` | run the packaged two-call battery, zero extra files |
| `team`, `export`, `benchmark` | aggregate or export local run data |
| `setup`, `describe`, `init` | print recording config, emit the CLI manifest, scaffold local integration files |
| `rubric run`, `rubric calibrate` | score a local transcript against a rubric with a **local** model (Ollama on `localhost`); opens no off-box socket by default, and the verdict cache is local too. Reaches the network only with an explicit hosted or non-local judge (see below) |
| `test run` (rubric lane) | same local judge as `rubric run`, run inline on a test's `assertions.rubric` lane. Local by default |
| `counterexample compile\|verify\|reproduce\|inspect\|export\|predicate` | read local scenario/test/capsule files, write a new local capsule or share-safe projection. Loads no provider adapter, model, subprocess, or network client |

Default retention is local-only: reports, envelopes, and exports land
exactly where you point them.

### Counterexample capsule boundary

A private `.hotato-repro` holds the source scenario and test, reduced
fixture, target assertion result, and proof journal -- treat it as
sensitive source material. The compiler creates it with owner-only
permissions where the host supports POSIX, never overwrites an existing
destination, and promotes a sibling staging directory only once every file
and hash exists.

The verifier rejects: path traversal; symlinked members and directories;
special files; undeclared files; oversized or deep inputs; digest
mismatches; broken deletion chains; evaluator-source drift on the strict
proof path; and a minimality claim that admits a preserving unit deletion.
Preflight caps a capsule at 1,024 files, 4,096 directories, 64 directory
levels, 64 MiB per member, and 256 MiB total. `reproduce` is a separate,
current-evaluator check: it permits evaluator drift, but still validates
capsule integrity and the source-to-final delete-only chain.

Replay also bounds proof-specific work: 256 KiB per selected assertion, 2
MiB per canonical candidate scenario, 256 KiB of rendered transcript text, 2
MiB per assertion result, 10,000 evidence rows, 512 accepted proof-chain
steps, 10,000 deletion operations per accepted step, and 512 remaining
deletion units in a completed minimality proof. Accepted steps require
fresh oracle evaluations -- cached preserved journal rows can't inflate the
chain. Proof regexes use a closed, fixed-width 1,024-byte subset that
refuses groups, alternation, backreferences, and variable quantifiers.
These limits apply to counterexample compilation and replay, not to global
evaluator limits. The selected-assertion and proof-regex byte checks run
before the general assertion validator can invoke Python's regex parser.

Each profile has an exact member allowlist; a manifest can't authorize a
new file outside it. For `share-safe-v1`, the only members are
`capsule.json`, `report.md`, `report.html`, `card.svg`, `README.md`, and
`MANIFEST.sha256.json` -- each human-facing file is reconstructed
canonically and compared byte for byte during inspection. This stops a
modified report, README, or card from being accepted just because its
replacement digest was written into a new manifest.

The canonical share renderer's bytes are frozen as part of the capsule v1
exchange contract. A future renderer must retain v1 output, or use a
versioned profile/format, rather than silently changing bytes under an
existing proof.

The evaluator digest binds the shipped Python source closure and package
version, not the interpreter build, host platform, CPU, or native
libraries. Strict replay compares the recorded result, content, and trace
hashes, so a runtime difference that changes behavior is rejected. Accepted
transforms and the final single-unit inventory are proof-bearing;
non-`PRESERVED` journal rows are diagnostic history, not independently
replayed.

The capsule directory must stay unchanged during a command. Persistent
mutation, root replacement, and symlink or special-file substitution are
all detected. A privileged local process that can swap and restore bytes
between individual reads sits outside the v1 proof snapshot -- move
untrusted capsules into a private, non-writable workspace before
verification.

`counterexample export` derives a non-runnable projection after strict
private verification. It omits scenario/test bodies, transcript content,
tool payloads, state values, provider identifiers, reducer paths, and
per-candidate digests; private minimality rows are reduced to aggregate
outcome counts. The projection exposes the payload-free selected failure
code for review while keeping its field/key/rule/detector/index
discriminator private. It still contains identifiers and hashes that
remain correlators -- low-entropy or externally known source material can
be tested against them. `share-safe-v1` belongs inside the team's normal
engineering-artifact access boundary; it does not claim anonymous public
disclosure.

## Network: only when you explicitly request it

These commands reach outside the machine, and only because reaching out is
their job. Each requires you to name a stack, a repository, or a webhook
you configured.

| Command | Network surface | Notes |
|---|---|---|
| `connect` | none at connect time | stores a stack's credentials locally at `0600`. Setup for the pull path; no audio moves |
| `pull` | your voice stack's API | bulk-fetch recent recordings from a stack **you** connected, into a local folder |
| `capture` | your voice stack's API | fetch and score one call from your stack |
| `sweep` | your voice stack's API | `pull` + analyze in one command |
| `inspect` | your voice stack's API | read (never write) the current turn-taking config. Read-only |
| `ingest` | a webhook endpoint you host | worker that scans each completed call. Payloads are data, not instructions (guarantee 3) |
| `issue` | GitHub, via your local `gh` | file a sweep's candidates as an issue. Uses your existing `gh` auth |
| `pr` | GitHub, via your local `gh` | open a PR adding promoted fixtures. Uses your existing `gh` auth |
| `apply` | a git clone you point it at | applies a patch to a fresh **staging** clone only, never the source. Dry-run by default; refuses a both-axes threshold funnel |
| `--diarizer pyannoteai` | Attention Labs hosted diarizer | the only audio path that can send audio off-box, and only with `--egress-opt-in`. The default diarizer is local |
| `--judge-provider hosted` / non-local `--judge-endpoint` (any rubric command) | a hosted or remote model host you name | sends the transcript + rubric criterion off-box for judging. Refused (exit 2) unless `--judge-egress-opt-in`. The default judge is a local Ollama model that stays on the box. See [`docs/EGRESS.md`](EGRESS.md) and [`docs/RUBRIC.md`](RUBRIC.md) |
| `test run --state` http adapter | your system-of-record's REST API | only when the state-config names `adapter: http`, and only with `egress_opt_in: true` in that config. Sends the mapped filter VALUES for one `state`/`state_change` query -- audio, transcript, and the config itself stay local. See [`docs/STATE-ADAPTERS.md`](STATE-ADAPTERS.md) |
| `test run --state` sql adapter over a `dsn` | your database, over the network | only when the state-config names `adapter: sql` with a `dsn`, and only with `egress_opt_in: true`. A parameterized, read-only SELECT with the mapped filter values bound as data. A local `sqlite_path` opens no socket |

The state adapters read a post-call **system of record** to ground an
Authority-2 `state` assertion (did the refund/appointment get written?). A
record the system of record can read, and does not hold, is a grounded
FAIL; a system of record Hotato could **not** reach or read (network error,
timeout, 5xx, non-JSON) is INCONCLUSIVE, never a guessed verdict.
Credentials come from environment-variable **names** in the config, keeping
the secret itself out of it; the HTTP adapter refuses a plain-`http://`
base URL unless `allow_http: true` is set for a trusted local endpoint. The
default `--state` path -- the local mock sandbox (a JSON/SQLite fixture)
and a local `sqlite_path` SQL DB -- runs entirely on-machine, no opt-in
needed.

Notify surfaces (Slack, GitHub) fire only through credentials you
configured (`gh`, a Slack token), and only for actions you invoked.

## Network trust: proxies and TLS

Every networked command above (`pull`, `capture`, `sweep`, `inspect`,
`apply`, and the credential probe in `connect`) makes its HTTP calls
through Python's standard `urllib`, which honors the `HTTP_PROXY` /
`HTTPS_PROXY` / `NO_PROXY` convention -- the same one `curl`, `pip`, `git`,
and `docker` follow. That's deliberate: it lets Hotato work behind a
corporate proxy with no extra flags. It also means an environment variable
set for the Hotato process controls where its outbound credentialed
requests go -- a trust decision worth stating plainly.

Two things bound how far that ambient trust reaches:

- **TLS certificate validation is always enforced.** Every credentialed
  base URL in `capture.py` and `apply.py` is hardcoded `https://`
  (`api.vapi.ai`, `api.retellai.com`, `api.twilio.com`, and the other
  supported stacks). A proxy set via `HTTP_PROXY`/`HTTPS_PROXY` can see the
  `CONNECT` target host and port, and can refuse or stall the connection (a
  denial of service) -- but reading or rewriting the `Authorization` header
  or the response body also requires a certificate the machine's own trust
  store already accepts for that vendor's domain: a much larger compromise
  (a rogue trusted root CA already installed), outside `HTTP_PROXY`'s reach
  and Hotato's control.
- **The threat prerequisite is already a compromised local environment.**
  An attacker able to set environment variables for the Hotato process can
  already read `VAPI_API_KEY` / `RETELL_API_KEY` / etc. straight out of the
  environment, or read the `0600` credentials file `connect` wrote -- both
  easier than standing up a TLS-valid proxy.

If you don't trust the ambient proxy environment a command will run in, set
`HOTATO_NO_PROXY=1` to make Hotato's HTTP opener ignore `HTTP_PROXY` /
`HTTPS_PROXY` for that run, or unset those variables first. The default is
unchanged: proxy env vars are honored, matching curl/pip/git, so a
corporate-proxy setup keeps working with no configuration.

## Hotato's containment guarantees

The three guarantees above, restated as the boundary they draw: recordings
stay local unless you opt in; a webhook payload is read as data and never
executed (guarantee 3); and the furthest a config change goes on its own is
a patch you apply yourself, to a staging clone.

## Verifying the posture

Confirm the posture yourself, without trusting this page:

- **Self-hosted, nothing to audit but the standard library.** `pip install
  hotato` pulls zero runtime dependencies; the only network code path is
  the one you explicitly invoke.
- **Credentials at `0600`.** `connect` writes stack credentials locally
  with owner-only permissions; check with `ls -l` on your credentials path.
- **Local-only retention.** Reports, envelopes, and exports exist exactly
  where you wrote them.
- **Egress is opt-in.** The only off-box audio path is `--diarizer
  pyannoteai`, gated by `--egress-opt-in` -- audio stays on the machine
  until you set it.

## Drive-a-call (`run_scenario`): placing a live call

`run_scenario` (`src/hotato/drive.py`, wired into the Vapi and Twilio
adapters) places an outbound call against a live agent and pulls the
recording. Its threat surface, and the controls on it:

- **A billable, outbound call always requires explicit opt-in.**
  `run_scenario` places the call only when BOTH live credentials AND an
  explicit egress opt-in (`HOTATO_DRIVE_OPT_IN=1` or `egress_opt_in: true`
  on the scenario) are present. Absent either, it raises a clean structured
  refusal and dials nothing -- the same opt-in posture as `--allow-mono` /
  `HOTATO_ALLOW_PRIVATE_URLS` / `--egress-opt-in`.
- **Production config stays untouched.** The surface is create-and-read
  only: the only verbs issued are `POST` (create the call) and `GET` (poll
  status, list the recording, download it) -- no `PUT`/`PATCH`/`DELETE`
  path to alter an assistant, phone number, or any other provider resource
  in place. For Vapi the call originates FROM the staging clone the
  experiment created, keeping the production source untouched.
- **The caller side is labelled precisely.** The produced conversation
  carries `origin.kind == "real"` with the provider and its call id, and
  `origin.caller` (`scripted-twiml` for the fixed-timeline Twilio caller,
  `assistant-originated` for a Vapi call the assistant placed) -- stating
  exactly what happened, with no claim that a human placed the call or that
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
fleet registry and evidence store. Its threat surface, and the controls on
it:

- **Localhost-default bind.** The server binds `127.0.0.1` unless you
  explicitly pass `--host`. A non-loopback bind (e.g. `--host 0.0.0.0`)
  always prints a prominent warning at start, and always requires that
  explicit flag.
- **Token auth on every request.** A shared bearer token authenticates
  every request, compared in constant time (`hmac.compare_digest`). It's
  either operator-supplied (`--token` / `--token-file`) or generated with
  `secrets.token_urlsafe` and stored `0600` under the per-workspace state
  dir. Browsers bootstrap an in-memory, HttpOnly session cookie from the
  printed `/?token=…` URL, then the token is stripped from the address bar
  via a redirect; it stays out of every response body. An unauthenticated
  request gets `401` before it's routed anywhere.
- **Read-only.** The server issues only `SELECT`s against the registry and
  reads evidence blobs by digest: no write endpoint, no workspace
  mutation. Reviews and labels stay CLI-driven. The only file it writes is
  the append-only audit log (`…/serve/<workspace>/audit.jsonl`, `0600`),
  recording who (token/session prefix, the secret stays out of it), what
  (method + path, token stripped from the query), when, and the response
  status of every request.
- **Zero egress.** The server only binds a listening socket: no outbound
  connection, nothing that phones home. Audio, traces, and evaluations
  stay on the machine. A test allowlists loopback and fails if any view
  attempts an external connection.
- **Evidence renders as data, never as executable content.** The raw
  evidence endpoint (`/evidence/<digest>`) serves blobs as `text/plain`
  with `X-Content-Type-Options: nosniff` (pages also set
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`), keeping a
  crafted evidence blob inert in the viewer. Redacted transcript/trace
  content is scrubbed at the data layer, so both the HTML and the JSON
  mirror render only the `[redacted]` placeholder.
- **Workspace id sanitized against path traversal.** The workspace id is
  used verbatim only in parameterized SQL; as a state-directory name it is
  sanitized to stay inside the registry home.

See [`docs/WORKSPACE.md`](WORKSPACE.md).

Security policy and reporting: [SECURITY.md](../SECURITY.md).
