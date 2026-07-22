<div align="center">

<img src=".github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypistats.org/packages/hotato"><img src="https://img.shields.io/pypi/dm/hotato?style=flat-square&color=c23c07&label=downloads" alt="Downloads per month"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg?branch=main" alt="CI status"></a></p>

# hotato

**The local-first AI engineering platform.**

Everything you reach for a hosted platform to do: trace, evaluate, test, and gate your LLM and voice agents, on your own machine. Free at any scale. Byte-reproducible. Nothing leaves it.

**[hotato.dev](https://hotato.dev)**

</div>

Hosted observability and eval platforms meter your traffic, keep your traces and prompts on their servers, and score your evals with a model, so the number drifts and cannot gate a build. hotato runs the same four jobs (tracing, evals, tests, and CI gates) on your own machine: free and MIT at any scale, byte-for-byte reproducible, and offline by default.

**Catch your first failure in seconds. One command, no account:**

```console
$ uvx hotato start --demo
Conversation failed: Agent did not yield; measured talk-over was 2.66 s.
    talk-over     2.66s   the agent kept talking while the caller held the floor
```

Your text eval read the words on that call and passed it. The timing failed. hotato scores what the transcript can't see, pins the catch as a CI contract, and reproduces the verdict byte for byte on every machine. It measures timing and say-do, not intent.

## What it does

Four planes, one install, nothing leaves your machine.

| | | |
| :-- | :-- | :-- |
| **Observe** | traces, tokens, cost, and latency, from the OTel spans you already emit | `hotato observe report traces/` |
| **Evaluate** | deterministic assertions plus a separated local-judge lane, no blended score | `hotato assert run` |
| **Test** | simulate calls, stress-test turn-taking, pin any failure as a fixture | `hotato gauntlet` |
| **Gate** | content-addressed contracts fail the build on a regression, in CI | `hotato contract verify` |

Deterministic. Byte-reproducible. Free, MIT. Agent-native over MCP.

Every verdict carries its evidence, scored across five dimensions: outcome, policy, conversation, speech, and reliability.

## Why it is different

Same four jobs a hosted platform runs. Three things it cannot offer.

| | hotato | Hosted platforms |
| :-- | :-- | :-- |
| Trace, evaluate, test, gate | yes | yes |
| Price at scale | free, MIT, any volume | metered per seat and per event |
| Verdicts | byte-for-byte reproducible, gate a build | vary run to run |
| Your traces and prompts | stay on your machine | live on their servers |
| Runs in CI, offline | yes | needs their service |

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

Keep it with `pipx install hotato`, drive it over MCP with `uvx --from "hotato[mcp]" hotato-mcp`, or walk the path in [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md).

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
      - uses: attenlabs/hotato@v1.14.0
        with:
          contracts: contracts/
          hotato-version: 1.14.0
```

Copy-paste workflow with a commit-SHA pin: [`docs/CI.md`](docs/CI.md).

## Feed it what you already have

Every onramp feeds the same offline scoring and the same `0` / `1` / `2` exit contract.

```bash
hotato pull --stack vapi --limit 10          # your stack's recorded calls
hotato trace ingest --otel traces.jsonl      # the OTel spans you already log
hotato simulate demo.scenario.json --out ./sim   # scripted fixtures, no production audio
```

Details: [`docs/CONNECT.md`](docs/CONNECT.md) &#183; [`docs/TRACE.md`](docs/TRACE.md) &#183; [`docs/SIMULATE.md`](docs/SIMULATE.md)

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo: it reads [`AGENTS.md`](AGENTS.md) and runs the loop end to end, offline, no key. The MCP server exposes the scorer plus read/verify/propose tools over local stdio ([`docs/MCP.md`](docs/MCP.md)).

## Nothing leaves your machine

hotato runs offline, on the machine that invokes it. The core is stdlib-only Python: no account, no key, no network call of its own. Your traces, prompts, and audio stay local, and the local-judge lane is opt-in and quality-gated, separate from the deterministic core.

## Specifications

| Property | Value |
| :-- | :-- |
| Footprint | ~10 MiB installed, 0 runtime dependencies (stdlib-only) |
| Reproducibility | byte-for-byte, content-addressed contract |
| Exit contract | `0` pass &#183; `1` fail &#183; `2` refuse |
| Release integrity | OIDC Trusted Publishing + build-provenance attested |
| Runtime | offline, off the production data path |

<details>
<summary><b>Verify the measurement yourself</b></summary>

```bash
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Provenance: [`corpus/real/README.md`](corpus/real) &#183; method: [`METHODOLOGY.md`](METHODOLOGY.md).

Timing is measurable only when the two voices arrive on separate channels; a mono or mixed export is marked **NOT SCORABLE** and refused (`hotato trust --stereo call.wav`).

</details>

## Contribute

Issues and PRs welcome: [`CONTRIBUTING.md`](CONTRIBUTING.md) &#183; [`SECURITY.md`](SECURITY.md) &#183; [`CHANGELOG`](CHANGELOG.md) &#183; [`docs/`](docs/)

## License

MIT ([`LICENSE`](LICENSE))

<div align="center"><sub>Know when to pass it on.</sub></div>

mcp-name: io.github.attenlabs/hotato
