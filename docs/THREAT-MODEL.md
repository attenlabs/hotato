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

Security policy and reporting: [SECURITY.md](../SECURITY.md).
