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
| `--diarizer pyannoteai` | Attention Labs hosted diarizer | The only path that can send audio off-box, and only with `--egress-opt-in`. The default diarizer is local. |

Notify surfaces (Slack, GitHub) are used only through credentials you configured
(`gh`, a Slack token) and only for actions you invoked. Hotato ships no default
integrations that fire on their own.

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
