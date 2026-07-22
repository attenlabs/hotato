<div align="center">

<img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypistats.org/packages/hotato"><img src="https://img.shields.io/pypi/dm/hotato?style=flat-square&color=c23c07&label=downloads" alt="Downloads per month"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg?branch=main" alt="CI status"></a></p>

# hotato: regression testing for voice agents

**[hotato.dev](https://hotato.dev)**

</div>

**Your voice agent is failing real calls. Your tests say it's fine.** Talk-over, dead air, a booking the agent confirmed and the backend never wrote. A text-level eval reads the words; it cannot see the timing.

hotato scores those failures from a two-channel recording or a timestamped transcript, then pins each catch as a CI contract that fails the build until it's fixed. Deterministic, offline, free.

**Catch your first failure in seconds. One command, no account:**

```console
$ uvx hotato start --demo
Conversation failed: Agent did not yield; measured talk-over was 2.66 s.
    talk-over     2.66s   the agent kept talking while the caller held the floor
```

It measures turn timing and say-do, not intent.

## What it does

Conversation QA across five dimensions (speech, conversation, outcome, policy, reliability), on your machine. Four things you do with it:

| | | |
| :-- | :-- | :-- |
| **Catch** | talk-over, dead air, and say-do gaps, from a recording or a transcript | `hotato investigate call.wav` |
| **Test** | simulate calls, stress-test turn-taking, pin any failure as a fixture | `hotato gauntlet` |
| **Gate** | content-addressed contracts fail CI on a regression | `hotato contract verify` |
| **Observe** | traces, tokens, cost, and latency, derived on your machine | `hotato observe report traces/` |

## From a bad call to a CI gate

One recording in. The pinned failure becomes a gate that stays red until the agent stops failing that call:

```console
$ hotato investigate ./call.wav
  most likely failure: [1] the agent talked over the caller for 2.66s
  next: hotato investigate label '.hotato/investigate-state.json#1' --expect yield

$ hotato investigate label '.hotato/investigate-state.json#1' --expect yield
  created hotato contract: call-8s-yield

$ hotato contract verify contracts/
  [FAIL] call-8s-yield  0/1 contracts pass; exit_code=1
```

A contract re-measures the captured failure under the pinned policy on every CI run, the same discipline a snapshot test gives you. Same input, same verdict, byte for byte, on every machine.

## Quickstart

```bash
# 1. catch a failure on two bundled calls (no account; exits 0)
uvx hotato start --demo
# 2. score your own recording (or a transcript: --transcript t.json)
hotato investigate ./call.wav
# 3. pin the caught moment as a regression contract
hotato investigate label '.hotato/investigate-state.json#1' --expect yield
# 4. gate every pull request on it
hotato contract verify contracts/
```

Keep it with `pipx install hotato`, drive it over MCP with `uvx --from "hotato[mcp]" hotato-mcp`, or walk the path in [`docs/GETTING-STARTED.md`](https://github.com/attenlabs/hotato/blob/main/docs/GETTING-STARTED.md).

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
      - uses: attenlabs/hotato@v1.13.1
        with:
          contracts: contracts/
          hotato-version: 1.13.1
```

Copy-paste workflow with a commit-SHA pin: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md).

## Feed it what you already have

Every onramp feeds the same offline scoring and the same `0` / `1` / `2` exit contract.

```bash
hotato pull --stack vapi --limit 10          # your stack's recorded calls
hotato trace ingest --otel traces.jsonl      # the OTel spans you already log
hotato simulate demo.scenario.json --out ./sim   # scripted fixtures, no production audio
```

Details: [`docs/CONNECT.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONNECT.md) &#183; [`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md) &#183; [`docs/SIMULATE.md`](https://github.com/attenlabs/hotato/blob/main/docs/SIMULATE.md)

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo: it reads [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) and runs the loop end to end, offline, no key. The MCP server exposes the scorer plus read/verify/propose tools over local stdio ([`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md)).

## Specifications

| Property | Value |
| :-- | :-- |
| Footprint | ~10 MiB installed, 0 runtime dependencies (stdlib-only) |
| Reproducibility | byte-for-byte, content-addressed contract |
| Exit contract | `0` pass &#183; `1` fail &#183; `2` refuse |
| Release integrity | OIDC Trusted Publishing + build-provenance attested |
| Runtime | offline, off the production audio path |

<details>
<summary><b>Verify the measurement yourself</b></summary>

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Provenance: [`corpus/real/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/real) &#183; method: [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md).

Timing is measurable only when the two voices arrive on separate channels; a mono or mixed export is marked **NOT SCORABLE** and refused (`hotato trust --stereo call.wav`).

</details>

## Contribute

Issues and PRs welcome: [`CONTRIBUTING.md`](https://github.com/attenlabs/hotato/blob/main/CONTRIBUTING.md) &#183; [`SECURITY.md`](https://github.com/attenlabs/hotato/blob/main/SECURITY.md) &#183; [`CHANGELOG`](https://github.com/attenlabs/hotato/blob/main/CHANGELOG.md) &#183; [`docs/`](https://github.com/attenlabs/hotato/blob/main/docs/)

## License

MIT ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE))

mcp-name: io.github.attenlabs/hotato
