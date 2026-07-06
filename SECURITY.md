# Security Policy

Hotato is offline by default. Scoring reads the local WAV files you point it at
and writes local files; no audio, transcript, or result leaves your machine.
Network access is used only when you explicitly run hosted-stack capture
commands. Core scoring, reports, exports, benchmarks, and demos run offline.

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
