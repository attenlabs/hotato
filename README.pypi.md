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
hotato check --demo             # a bundled failing call: what broke, and when
hotato check ./call.wav         # then your own recording
hotato vapi health              # or your last 100 Vapi calls
```

Zero config. Vapi, Retell, Bland, Synthflow, Millis, or local audio.
Works with any recording: mono, dual-channel, or a transcript. Timing math, not a judge. Offline. Free. MIT.

**[hotato.dev](https://hotato.dev)**

</div>

## What it finds

- **Say-do gaps**: the caller interrupts to cancel (a barge-in), the agent says "canceled", the booking tool fires anyway. hotato takes the turn timing from the audio and the tool call from your OTel trace.
- **Latency spikes**: the pause before a reply going from 800 ms to over 2 s.
- **Dead air**: that pause reaching 5 s, or the line going quiet.
- **Talk-over**: the agent starts a fresh utterance over the caller.

## Quickstart

### Vapi

```bash
pip install hotato
export VAPI_API_KEY=...
hotato vapi health --last 7d --output report.html
```

Open `report.html`: every critical incident, timestamped, and your Voice Stability Score.

### Retell

```bash
export RETELL_API_KEY=...
hotato retell health --call-id CALL_ID
```

`--call-id` is required and repeatable: you name the Retell calls to pull.
`hotato bland health`, `hotato synthflow health`, and `hotato millis health`
follow the Vapi shape. Those stacks mix both voices onto one channel, so
they measure silence timing, dead air and latency gaps, each finding with
its measured confidence; barge-in and talk-over need two channels.

### Local audio

```bash
hotato autopsy ./call.wav
```

Writes a self-contained HTML report to `hotato-output/`, plus the JSON
`pin` reads. Open the HTML in your browser.

## From finding bugs to preventing them

`autopsy` turns one recording into timestamped incidents. `scan` reads a
folder and tracks the trend. When you are ready, move a finding into CI:
`hotato pin` turns one incident into a portable failure check, and `hotato
prove` re-runs every stored check and fails the build rather than pass on
evidence it cannot re-read. Every verdict carries its own evidence across
five dimensions: outcome, policy, conversation, speech, reliability.

[Pin a bug →](https://github.com/attenlabs/hotato/blob/main/docs/CI.md)

For continuous use: run `hotato vapi health` on a schedule, and open
`hotato console --production-db evidence.db` to watch calls land live.

## Wire it into CI

The exit code **is** the verdict: `0` pass, `1` fail, `2` refuse (could not tell).

```yaml
# .github/workflows/voice-qa.yml
on: [pull_request]
jobs:
  hotato:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: attenlabs/hotato@v1.18.1
        with:
          contracts: contracts/
          hotato-version: 1.18.1
```

`contracts/` holds what `pin` wrote. Full workflow: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md).

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo: it reads
[`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) and runs the loop end to end offline, no key. Over local
stdio the MCP server adds the scorer plus read/verify/propose tools:
`uvx --from "hotato[mcp]" hotato-mcp` ([`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md)). Deploying is yours.

## Nothing leaves your machine

hotato runs offline, on the machine that invokes it. The core is stdlib-only
Python: no account, no key, no network call of its own. Your traces, prompts,
and audio stay on your disk. Scoring with a language model you host yourself
is a separate opt-in add-on, outside that core.

## Go deeper

The whole loop, command by command: [`docs/LIFECYCLE.md`](https://github.com/attenlabs/hotato/blob/main/docs/LIFECYCLE.md).
First touch to a CI gate: [`docs/GETTING-STARTED.md`](https://github.com/attenlabs/hotato/blob/main/docs/GETTING-STARTED.md).
Feed it what you already have: [`docs/CONNECT.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONNECT.md) &#183;
[`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md) &#183; [`docs/SIMULATE.md`](https://github.com/attenlabs/hotato/blob/main/docs/SIMULATE.md).
What every verdict stands on: [`docs/EVIDENCE-CONTRACT.md`](https://github.com/attenlabs/hotato/blob/main/docs/EVIDENCE-CONTRACT.md).
Next to the hosted alternatives: [`docs/COMPARE.md`](https://github.com/attenlabs/hotato/blob/main/docs/COMPARE.md).

The deep toolkit -- capture, simulation, load, benchmarking, the fix ladder,
the fleet control plane -- lives under `hotato lab` (`hotato lab --help`).
The public commands are durable. hotato lab moves faster, and every
command name that worked before 1.17 still runs unchanged.

## Specifications

| Property | Value |
| :-- | :-- |
| Footprint | ~10 MiB installed, 0 runtime dependencies (stdlib-only) |
| Reproducibility | byte-for-byte: the same recording, the same report |
| Exit codes | `0` pass &#183; `1` fail &#183; `2` refuse |
| Release integrity | OIDC Trusted Publishing + build-provenance attested |
| Runtime | offline, off the production data path |

<details>
<summary><b>Verify the measurement yourself</b></summary>

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Provenance: [`corpus/real/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/real/README.md) &#183; method: [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md).

Timing is measurable only when the two voices arrive on separate channels; a mono or mixed export is marked **NOT SCORABLE** and refused (`hotato trust --stereo call.wav`). The full four-tier evidence policy (what each verdict stands on, per input) is [`docs/EVIDENCE-CONTRACT.md`](https://github.com/attenlabs/hotato/blob/main/docs/EVIDENCE-CONTRACT.md).

</details>

## Contribute

Issues and PRs welcome: [`CONTRIBUTING.md`](https://github.com/attenlabs/hotato/blob/main/CONTRIBUTING.md) &#183; [`SECURITY.md`](https://github.com/attenlabs/hotato/blob/main/SECURITY.md) &#183; [`CHANGELOG`](https://github.com/attenlabs/hotato/blob/main/CHANGELOG.md) &#183; [`docs/`](https://github.com/attenlabs/hotato/blob/main/docs/)

## License

MIT ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE))

<div align="center"><sub>Know when to pass it on.</sub></div>

mcp-name: io.github.attenlabs/hotato
