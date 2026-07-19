<div align="center">

<img src=".github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg?branch=main" alt="CI status"></a></p>

# hotato: regression testing for voice agents

**[hotato.dev](https://hotato.dev)**

</div>

**The transcript passed. The call failed.** The agent talked over the caller. None of it is in the words. Hotato is self-hosted conversation QA for voice agents: it scores the turn timing from a two-channel recording, checks what the agent *said* against what the backend *did*, and gates CI with exit `0` or `1`.

Your platform has the audio: `hotato pull --stack vapi` fetches the two-channel recording.

## See the loop catch a regression

One recording in. The pinned failure becomes a CI gate:

```console
$ hotato investigate ./call.wav
  most likely failure: [1] t=7.63s agent_stop_no_caller
  next: hotato investigate label '.hotato/investigate-state.json#1' --expect yield

$ hotato investigate label '.hotato/investigate-state.json#1' --expect yield
created hotato contract: call-8s-yield  passed: False

$ hotato contract verify contracts
  [FAIL] call-8s-yield (expect yield): did_yield=False talk_over=0.00s
  0/1 contracts pass; exit_code=1
```

A committed contract is a pinned bad call: it is *meant* to stay exit `1` until you fix the agent and recapture, the same way a snapshot test stays red until you update the snapshot.

## Quickstart

Zero setup, no account. The five commands are the whole path, first touch to a CI gate that guards every pull request:

```bash
# 1. see it catch a failure on two bundled calls (no account; this step exits 0)
uvx hotato start --demo
# 2. score your own two-channel recording
hotato investigate ./call.wav
# 3. commit the caught moment as a regression contract
hotato investigate label '.hotato/investigate-state.json#1' --expect yield
# 4. open the pull request that adds the CI gate
hotato pr create --fixtures contracts/<id>.hotato --repo OWNER/REPO --title 'Add hotato contract <id>'
# 5. the gate re-runs the stored evidence (exits 1 while the pinned call stays red)
hotato contract verify contracts/
```

Install with `pipx install hotato`, drive it over MCP with `uvx --from "hotato[mcp]" hotato-mcp`, or walk the path step by step in [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md).

## How it works

```text
two-channel recording
  ->  measure turn timing + verify say-do from the trace
  ->  content-addressed contract
  ->  CI verdict: exit 0 pass / exit 1 fail
```

A catch becomes a contract addressed by its own content, so the exact failure reproduces on any machine that runs the suite.

## What it scores

Each dimension scores on its own, then rolls up into one pass/fail verdict.

| Dimension | What it scores |
| :-- | :-- |
| ⏱️ **Speech** | Response latency and turn timing, measured from the two channels. |
| 💬 **Conversation** | Did the agent yield when the caller took the floor, and how fast. |
| 🎯 **Outcome** | Was the job done, judged on tool-call and state evidence. |
| 📋 **Policy** | Required disclosures and PII handling. |
| 📈 **Reliability** | `pass@1` / `pass@k` / `pass^k` with a Wilson interval. |

Timing comes straight from the two channels; say-do reads your `voice_trace.v1` spans, so what the agent told the caller is checked against what the backend did.

## Other ways to feed it

Every onramp feeds the same offline scoring and the same `0` / `1` / `2` exit contract.

**Traces you already log (no audio needed).** Wire the OTel spans you already emit into the same say-do check:

```bash
hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
hotato assert run --trace voice_trace.jsonl --transcript call.transcript.json --assertions assertions.yaml
```

Details: [`docs/ASSERTIONS.md`](docs/ASSERTIONS.md) &#183; [`docs/TRACE.md`](docs/TRACE.md) &#183; ground truth: [`examples/reference-agent`](examples/reference-agent), a 375-run offline suite.

**Your stack's recorded calls.** Vapi, Twilio, and Retell fetch a separated two-channel file; everything scores offline afterwards.

```bash
hotato pull --stack vapi --limit 10
```

Details: [`docs/CONNECT.md`](docs/CONNECT.md) &#183; drive a call against your live agent: [`docs/DRIVE-A-CALL.md`](docs/DRIVE-A-CALL.md)

**Scripted fixtures (no production audio).** A deterministic scripted caller renders a `scenario.v1` labelled `origin=simulated`; a seeded replay is byte-identical.

```bash
hotato simulate --init demo.scenario.json && hotato simulate demo.scenario.json --out ./sim
```

Details: [`docs/SIMULATE.md`](docs/SIMULATE.md)

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo: it reads [`AGENTS.md`](AGENTS.md) and runs the spine itself, offline, no key.

```text
"Try hotato on the calls in ./recordings and add a CI gate that fails the build on a talk-over regression."
```

## Wire it into CI

The step's exit code **is** hotato's verdict:

```yaml
# .github/workflows/voice-qa.yml
name: voice qa
on: [pull_request]
permissions:
  contents: read          # read-only; runs fully offline
jobs:
  hotato:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: attenlabs/hotato@v1.9.0
        with:
          contracts: contracts/          # the catches you committed
          hotato-version: 1.9.0          # exact pin, never a range
```

<details>
<summary><b>Exit-code contract (gate on this, do not parse stdout)</b></summary>

| Exit | Meaning |
| :-: | :-- |
| `0` | every scorable event passed |
| `1` | a scorable event regressed |
| `2` | usage error or unusable input (bad flags, corrupt file, mono recording, or no scorable event) |

Copy-paste workflow with commit-SHA pin: [`docs/CI.md`](docs/CI.md) &#183; [`docs/CONTRACTS.md`](docs/CONTRACTS.md).

</details>

## Drive it over MCP

The MCP server from Quickstart exposes the `voice_eval_run` scorer plus read/verify/propose tools to any MCP client over local stdio. Setup: [`docs/MCP.md`](docs/MCP.md).

## Specifications

| Property | Value |
| :-- | :-- |
| Footprint | ~10 MiB installed |
| Core dependencies | 0 (stdlib-only) |
| Reproducibility | byte-for-byte, content-addressed contract |
| Exit contract | `0` pass · `1` fail · `2` refuse |
| Release integrity | OIDC Trusted Publishing + build-provenance attested |
| Runtime | offline, off the production audio path |

## Verify the measurement yourself

<details>
<summary><b>Re-run the measurement benchmark</b></summary>

```bash
# re-run the measurement-error benchmark on the recorded AMI clips
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Provenance and caveats: [`corpus/real/README.md`](corpus/real) &#183; method: [`METHODOLOGY.md`](METHODOLOGY.md).

</details>

<details>
<summary><b>Two channels, one party each</b></summary>

Timing between two voices is measurable only when they arrive on separate channels; a mono or mixed export is marked **NOT SCORABLE** and refused. It measures timing, not intent: a person labels each candidate moment yield or hold.

```bash
hotato trust --stereo call.wav        # per-channel activity, swap flag, scorability
```

</details>

## Contribute

Issues and PRs are welcome: [`CONTRIBUTING.md`](CONTRIBUTING.md) &#183; [`SECURITY.md`](SECURITY.md) &#183; [`CHANGELOG`](CHANGELOG.md)

**Docs:** [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) &#183; [`AGENTS.md`](AGENTS.md) &#183; [`METHODOLOGY.md`](METHODOLOGY.md) &#183; [`docs/START.md`](docs/START.md) &#183; [`docs/CI.md`](docs/CI.md) &#183; [`docs/CONTRACTS.md`](docs/CONTRACTS.md) &#183; [`docs/MCP.md`](docs/MCP.md)

## License

MIT ([`LICENSE`](LICENSE))

mcp-name: io.github.attenlabs/hotato
