# Security Policy

## Posture

Hotato has **no hosted backend and no telemetry**. `pip install hotato` pulls
zero runtime dependencies, so the core is standard-library Python with nothing
to phone home.

Your call recordings stay on your machine. Scoring, scanning, input-health
checks, reports, fixtures, comparison, verification, diagnosis, plans, patches,
and demos are **offline**: they read the local files you point them at and write
local files, and open no sockets.

Network access happens only when you explicitly run a command whose job is to
reach out, and only against a stack, repository, or webhook you configured:

- `connect` stores a stack's credentials locally at `0600` (no audio moves).
- `pull`, `capture`, and `sweep` fetch recordings from a stack **you** connected.
- `inspect` reads (never writes) a stack's turn-taking config.
- `ingest` runs a webhook worker you host; it treats payloads as data, never as
  instructions.
- `issue` and `pr` act on GitHub through your existing local `gh` auth.
- `apply` operates on a fresh staging clone only, dry-run by default; it never
  mutates the source or a production stack.

**Hotato never mutates production by default, never uploads your recordings to
Attention Labs, and never uses a webhook payload as instructions.** The single
off-box audio path is the hosted `--diarizer pyannoteai` backend, which requires
an explicit `--egress-opt-in` flag; the default diarizer is local.

Full detail, command by command: [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md). A
per-command network table derived directly from the code (which commands are
fully local, which reach your configured vendor and when, which optional
extras add a hosted call): [`docs/EGRESS.md`](docs/EGRESS.md).

## Reporting

Do not open a public GitHub issue for:

- vulnerabilities in Hotato or its packaging
- anything involving call recordings, consent, PII, PHI, or private audio

Email: security@attentionlabs.ai

Include what you found, how to reproduce it, and your version
(`hotato --version`). You will get an acknowledgement within 3 business days.

## Supported versions

Security fixes land in the latest release; upgrade to the newest version and
confirm the issue reproduces there before reporting.
