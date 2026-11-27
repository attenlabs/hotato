# Security packet

This page is for a security or procurement reviewer deciding whether hotato
can run against their call data. It answers five questions on one page: what
licence the code is under, where call data goes, how to check that a release
is the code in this repository, what the threat model covers, and how to report
a vulnerability. Each answer links to the longer page it summarises.

hotato is a command-line tool and Python package that tests voice agents. It
scores call recordings, transcripts and traces on your own machine. There is
no hosted hotato service and no telemetry.

## At a glance

| Question | Answer | Detail |
|---|---|---|
| Licence | MIT | [`LICENSE`](../LICENSE) |
| Runtime dependencies | none; the core is standard-library Python | [`pyproject.toml`](../pyproject.toml) |
| Telemetry | none | [`SECURITY.md`](../SECURITY.md#posture) |
| Where call data is processed | on your machine, in the files you point hotato at | [`EGRESS.md`](EGRESS.md) |
| Network access | only from commands whose job is to reach a system you configured | [`EGRESS.md`](EGRESS.md) |
| Audio leaving your machine | one opt-in path: the hosted diarizer, behind `--egress-opt-in` | [`THREAT-MODEL.md`](THREAT-MODEL.md#three-guarantees) |
| Build provenance | GitHub build-provenance attestation over the wheel, the sdist and the SBOMs | [Provenance](#provenance-and-sbom) |
| Bill of materials | CycloneDX SBOM, one for the whole package and one per optional extra | [Provenance](#provenance-and-sbom) |
| Vulnerability reports | private, through the contact in `SECURITY.md`; acknowledged within 3 business days | [`SECURITY.md`](../SECURITY.md#reporting) |

## Licence

hotato is released under the MIT licence (`license = "MIT"` in
`pyproject.toml`, full text in [`LICENSE`](../LICENSE)). The package has no
runtime dependencies. The optional extras (for example `hotato[transcribe]` or
`hotato[judge]`) pull in third-party packages under their own licences, and
each extra's SBOM lists them.

## Data flow

By default everything stays on the machine that runs hotato:

- Scoring, scanning, reports, fixtures, assertions and contract checks read
  local files and write local files. They open no network socket.
- The model-judged rubric lane uses a local Ollama model
  (`http://localhost:11434`) by default. The transcript stays on your machine.
- Stack credentials saved by `hotato connect` are stored locally with
  owner-only permissions (`0600`).
- Reports, result envelopes and exports are written only where you point them.

Data leaves the machine only when you run a command whose job is to reach a
system you named:

| What you run | What it reaches | What leaves |
|---|---|---|
| `pull`, `capture`, `sweep`, `monitor run` | your voice platform's API | requests for your own recordings; audio comes back to your disk |
| `monitor run` alerts, `--notify URL` | the Slack, PagerDuty or webhook destination you set | call id, rule, scores and timing numbers; never audio, credentials or transcript text |
| `issue`, `pr` | GitHub, through your own `gh` login | the issue or pull request content you asked for |
| `simulate --chat URL` | your chat agent; a non-local URL needs `--egress-opt-in` | the scripted caller turns |
| `--judge-provider hosted`, or a non-local judge endpoint | the model endpoint you name; needs `--judge-egress-opt-in` | the transcript and the rubric criterion |
| `--diarizer pyannoteai` | the hosted diarization service; needs `--egress-opt-in` | the mono recording |

Every credentialed endpoint is `https://` and certificate checks are never
turned off. Standard proxy variables are honoured; `HOTATO_NO_PROXY=1` ignores
them. The complete command-by-command table, with the source file for each
network call, is [`EGRESS.md`](EGRESS.md).

Code you add runs locally too. Custom metric plugins
([`CUSTOM-METRICS.md`](CUSTOM-METRICS.md)) are Python files that hotato imports
only when an assertion names one, so point hotato only at plugin code you trust.

## Provenance and SBOM

Releases to PyPI go through the Trusted Publishing workflow
([`.github/workflows/publish-pypi-oidc.yml`](../.github/workflows/publish-pypi-oidc.yml)):

1. The package is built twice and the two builds are compared. A difference
   fails the run.
2. CycloneDX SBOMs are generated from `pyproject.toml` and validated: one for
   the whole package (`hotato.sbom.cdx.json`) and one per dependency profile
   (`hotato.sbom.<profile>.cdx.json`, the core plus each optional extra).
3. A separate, gated publish job downloads those exact files, writes a GitHub
   build-provenance attestation over the wheel, the sdist and each SBOM, and
   uploads the wheel and sdist to PyPI with a short-lived token. No long-lived
   PyPI credential exists.

Where to find them:

- the wheel and sdist: [PyPI](https://pypi.org/project/hotato/), and attached
  to each GitHub release;
- the SBOMs: attached to each GitHub release, and kept by the workflow run as
  the `hotato-sbom` artifact;
- the attestations: the repository's Attestations page on GitHub.

To check a file you downloaded:

```bash
gh attestation verify hotato-<version>-py3-none-any.whl --repo attenlabs/hotato
gh attestation verify hotato.sbom.cdx.json --repo attenlabs/hotato
python3 scripts/gen_sbom.py --check hotato.sbom.cdx.json
```

The SBOM lists the dependencies the package declares, with their declared
version ranges. It does not resolve or pin the full transitive set, and it
says so in its own metadata (`hotato:dependency-scope = declared-direct`,
`hotato:transitive-resolved = false`).

Other supply-chain controls:

- Every GitHub Action in the workflows is pinned to a full commit SHA, and a
  test fails when one is not. Dependabot proposes new pins weekly.
- CodeQL scans the Python code on every pull request, on pushes to `main`, and
  weekly.

The release steps are listed in
[`RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md).

## Threat model

The full model is [`THREAT-MODEL.md`](THREAT-MODEL.md). Its three guarantees:

1. **You apply every production change yourself.** `plan` and `patch` produce
   a proposal; `apply` runs on a fresh staging clone and is a dry run by
   default. No command pushes to production on its own.
2. **Recordings stay on your machine.** Audio moves from your voice platform to
   your disk only when you run `pull`, `capture` or `sweep`. The one exception
   is the hosted diarizer, which needs `--egress-opt-in`.
3. **A webhook payload is data, never code.** `ingest` reads a completed-call
   notification and scans the recording it names. Payload fields are never
   executed or used to choose what runs.

The threat model also covers the self-hosted team workspace (`hotato serve`):
it binds `127.0.0.1` unless you pass `--host`, every request needs a token,
and its one write route is protected against cross-site requests. Placing a
live test call (`hotato drive`) needs an explicit confirmation.

Recordings themselves carry sensitive content (names, account numbers,
voices). hotato's redaction hides metadata, not spoken words.
[`SECURITY.md`](../SECURITY.md#audio-handling) explains what that means for
sharing reports and contract bundles.

## Reporting a vulnerability

Report privately, never in a public issue, using the contact in
[`SECURITY.md`](../SECURITY.md#reporting). Include what you found, how to
reproduce it, and the output of `hotato --version`. You will get an
acknowledgement within 3 business days. Fixes land in the latest release.
