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

## Audio handling

Every command that touches a real recording (`run`, `capture`, `fixture
create`, `contract create`, `report`, `doctor`, `verify`, `fix trial`) reads
and writes the actual audio bytes, not just metadata about them. Raw call
audio routinely carries:

- names, account numbers, and other identifiers spoken aloud;
- health, financial, or other regulated information a caller states out loud;
- answers to authentication questions (a security question, a card number, a
  one-time code read back);
- the caller's and the agent's voices themselves, which some jurisdictions
  treat as biometric data;
- whatever a recording-consent law in your jurisdiction required notice or
  consent for before the call was captured. That obligation attaches to the
  raw audio you already have; Hotato does not add it and does not remove it.

**Redaction hides metadata, not spoken content.** `contract create`'s default
redaction (leaving off `--include-identifiers`) hides a candidate ref and a
source recording's basename from `contract.json`, `source/call_metadata.json`,
and `evidence/card.svg`. It never touches `audio/event.wav`. Nothing Hotato
ships transcribes, redacts, or bleeps the audio itself: every word either
party said is still audible in the bundle exactly as recorded.

**Do not commit a production contract bundle, or a report built with
`--embed-audio`, to a public repository.** `hotato doctor` on a real
recording sets `--embed-audio` by default; see `docs/REPORTS.md` for the same
caution scoped to reports. Use sanitized fixtures (synthetic or
consent-cleared) for anything public, and keep a real-customer bundle or
embedded-audio report in a private repository or controlled artifact storage.

**A self-contained bundle or report is technically portable, not approved
for distribution.** `contract pack` producing one `.hotato` archive, or
`--embed-audio` producing one `.html` file, makes something easy to attach to
an email, a chat message, or a public issue, and that portability is a
packaging property, not a review of what is inside it. Give it the same
distribution judgment you would give the raw recording, because the raw
recording is inside it.

**When you need to share proof without sharing the recording, prefer an
audio-free evidence summary**: the contract id, the measured timing values,
the trust (input-health) result, the human label, the evidence hashes
(`source.source_audio_sha256` in `contract.json`, or a run envelope's
`audio_provenance` sha256), the contract schema revision that produced it,
and the pass/fail outcome. `hotato contract inspect --format json` and
`hotato contract verify --format json` already carry every one of those
fields without the audio; reach for the bundle or an embedded-audio report
only once that is not enough.

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
