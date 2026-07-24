<div align="center">

<img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypistats.org/packages/hotato"><img src="https://img.shields.io/pypi/dm/hotato?style=flat-square&color=c23c07&label=downloads" alt="Downloads per month"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg?branch=main" alt="CI status"></a>
<a href="https://github.com/attenlabs/hotato/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/hotato?style=flat-square&color=6f5d44" alt="MIT license"></a></p>
<!-- Add a stars badge (shields.io github/stars/attenlabs/hotato) here once the repo reaches ~25 stars; below that it advertises the low number. -->

# hotato

**Find what broke in your agent calls. Pin it so it never ships again.**

```bash
pip install hotato
hotato vapi health              # analyze your last 100 Vapi calls
hotato autopsy ./call.wav       # or analyze one local file
```

Zero config. Works with Vapi, Retell, Bland, Synthflow, Millis, or local audio.
No judges. No cloud. No bill. MIT.

**[hotato.dev](https://hotato.dev)**

</div>

## What it finds

- **Barge-in → Say-do gaps**: Caller interrupts to cancel; agent says "canceled" but the booking tool still fires: a bug that fires actions the caller canceled. (Timing from the audio; the tool-fire check reads your call's tool log: hotato ingests Vapi/OTel traces.)
- **Latency spikes**: 800ms → 5s unpredictability that makes users hang up.
- **Dead air**: Long silences that kill conversation flow.
- **Talk-over**: Agent speaks over the caller; never yields.

## Quickstart

### Vapi

```bash
pip install hotato
export VAPI_API_KEY=...
hotato vapi health --last 7d --output report.html
```

Open `report.html`. See your Voice Stability Score and every critical incident.

### Retell

```bash
export RETELL_API_KEY=...
hotato retell health --call-id CALL_ID
```

`--call-id` is required and repeatable: Retell has no verified
list-recent-calls endpoint, so hotato never guesses one. `hotato bland health`,
`hotato synthflow health`, and `hotato millis health` follow the Vapi shape;
those stacks export one mixed channel, so their reports carry the
measured-confidence mono observations block.

### Local audio

```bash
hotato autopsy ./call.wav
```

Writes a detailed, self-contained HTML incident report under `hotato-output/`;
open it in your browser.

## From finding bugs to preventing them

`autopsy` finds bugs. `scan` tracks trends across a folder of calls. When you
are ready, pin incidents to your CI so they never ship again: `hotato pin`
turns one incident into a portable failure check, and `hotato prove` is the CI
check that re-runs every stored piece of evidence and fails closed. Every
verdict carries its evidence across five dimensions (outcome, policy,
conversation, speech, reliability).

[Read more →](https://github.com/attenlabs/hotato/blob/main/docs/CI.md)

For continuous use: run `hotato vapi health` on a schedule, and open
`hotato console --production-db DB` to inspect stored runs locally.

## Wire it into CI

The step's exit code **is** the verdict: `0` pass, `1` fail, `2` refuse.

```yaml
# .github/workflows/voice-qa.yml
on: [pull_request]
jobs:
  hotato:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: attenlabs/hotato@v1.16.0
        with:
          contracts: contracts/
          hotato-version: 1.16.0
```

Copy-paste workflow with a commit-SHA pin: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md).

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo: it reads
[`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) and runs the loop end to end, offline, no key. The MCP
server exposes the scorer plus read/verify/propose tools over local stdio:
`uvx --from "hotato[mcp]" hotato-mcp` ([`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md)).

## Nothing leaves your machine

hotato runs offline, on the machine that invokes it. The core is stdlib-only
Python: no account, no key, no network call of its own. Your traces, prompts,
and audio stay local, and the local-judge lane is opt-in and quality-gated,
separate from the deterministic core.

## Go deeper

The whole loop, command by command: [`docs/LIFECYCLE.md`](https://github.com/attenlabs/hotato/blob/main/docs/LIFECYCLE.md).
First touch to a CI gate: [`docs/GETTING-STARTED.md`](https://github.com/attenlabs/hotato/blob/main/docs/GETTING-STARTED.md).
Feed it what you already have: [`docs/CONNECT.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONNECT.md) &#183;
[`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md) &#183; [`docs/SIMULATE.md`](https://github.com/attenlabs/hotato/blob/main/docs/SIMULATE.md).
What every verdict stands on: [`docs/EVIDENCE-CONTRACT.md`](https://github.com/attenlabs/hotato/blob/main/docs/EVIDENCE-CONTRACT.md).
Next to the hosted alternatives: [`docs/COMPARE.md`](https://github.com/attenlabs/hotato/blob/main/docs/COMPARE.md).

The deep toolkit -- capture, simulation, load, benchmarking, the fix ladder,
the fleet control plane -- lives under `hotato lab` (`hotato lab --help`).
The public commands are durable; `hotato lab` evolves faster; every pre-1.17
top-level spelling keeps working unchanged.

## Specifications

| Property | Value |
| :-- | :-- |
| Footprint | ~10 MiB installed, 0 runtime dependencies (stdlib-only) |
| Reproducibility | byte-for-byte, content-addressed checks |
| Exit codes | `0` pass &#183; `1` fail &#183; `2` refuse |
| Release integrity | OIDC Trusted Publishing + build-provenance attested |
| Runtime | offline, off the production data path |

<details>
<summary><b>Verify the measurement yourself</b></summary>

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Provenance: [`corpus/real/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/real) &#183; method: [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md).

Timing is measurable only when the two voices arrive on separate channels; a mono or mixed export is marked **NOT SCORABLE** and refused (`hotato trust --stereo call.wav`). The full four-tier evidence policy (what each verdict stands on, per input) is [`docs/EVIDENCE-CONTRACT.md`](https://github.com/attenlabs/hotato/blob/main/docs/EVIDENCE-CONTRACT.md).

</details>

## Contribute

Issues and PRs welcome: [`CONTRIBUTING.md`](https://github.com/attenlabs/hotato/blob/main/CONTRIBUTING.md) &#183; [`SECURITY.md`](https://github.com/attenlabs/hotato/blob/main/SECURITY.md) &#183; [`CHANGELOG`](https://github.com/attenlabs/hotato/blob/main/CHANGELOG.md) &#183; [`docs/`](https://github.com/attenlabs/hotato/blob/main/docs/)

## License

MIT ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE))

<div align="center"><sub>Know when to pass it on.</sub></div>

mcp-name: io.github.attenlabs/hotato
