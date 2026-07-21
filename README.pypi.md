<div align="center">

<img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypistats.org/packages/hotato"><img src="https://img.shields.io/pypi/dm/hotato?style=flat-square&color=c23c07&label=downloads" alt="Downloads per month"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://github.com/attenlabs/hotato/actions/workflows/tests.yml/badge.svg?branch=main" alt="CI status"></a></p>

# hotato: regression testing for voice and chat agents

**[hotato.dev](https://hotato.dev)**

</div>

**The transcript read clean. The call still failed.** Talk-over. Dead air. A booking the agent confirmed and the backend never wrote.

Hotato measures turn timing and say-do evidence from a two-channel recording or a timestamped transcript, then pins each catch as a content-addressed CI contract.

Same input. Same verdict. Byte for byte. Runs locally. Free, MIT, no account.

**Byte-reproducible verdicts** &#183; **content-addressed contracts** &#183; **git-bisect predicates** &#183; **agent-native over local MCP**

**First catch in seconds, one command:**

```console
$ uvx hotato start --demo
...
Conversation failed: Agent did not yield; measured talk-over was 0.25 s.
    talk-over     0.25s  seconds the agent kept talking while the caller held the floor
    response gap  2.18s  seconds of dead air from the caller's turn end to the reply
```

It measures turn timing and say-do, not intent.

## Catch a failure on your own call

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

A contract preserves the captured failure and re-measures it under the pinned policy on every CI run, the same discipline a snapshot test gives you: the check stays red until the measured behavior changes.

## Quickstart

The five commands are the whole path, first touch to a CI gate that guards every pull request:

```bash
# 1. see it catch a failure on two bundled calls (no account; this step exits 0)
uvx hotato start --demo
# 2. score your own recording (or a timestamped transcript: --transcript t.json)
hotato investigate ./call.wav
# 3. commit the caught moment as a regression contract
hotato investigate label '.hotato/investigate-state.json#1' --expect yield
# 4. open the pull request that adds the CI gate
hotato pr create --fixtures contracts/<id>.hotato --repo OWNER/REPO --title 'Add hotato contract <id>'
# 5. the gate re-runs the stored evidence (exits 1 while the pinned call stays red)
hotato contract verify contracts/
```

Install with `pipx install hotato`, drive it over MCP with `uvx --from "hotato[mcp]" hotato-mcp`, or walk the path step by step in [`docs/GETTING-STARTED.md`](https://github.com/attenlabs/hotato/blob/main/docs/GETTING-STARTED.md).

## How it works

```text
two-channel recording, or timestamped transcript
  ->  measure turn timing + verify say-do from the trace
  ->  content-addressed contract
  ->  CI verdict: exit 0 pass / exit 1 fail
```

A catch becomes a contract addressed by its own content, so the exact failure reproduces on any machine.

Everything inside maps to four workflows, feeding five scored dimensions (speech, conversation, outcome, policy, reliability):

| Workflow | What it covers |
| :-- | :-- |
| **Catch** | recordings, transcripts, traces, acoustic signals, failure clustering |
| **Exercise** | personas, chat simulation, robustness batteries, scenario variables and branches |
| **Decide** | deterministic assertions, formulas, policy packs, reliability |
| **Gate** | contracts, baseline drift, PR summaries, release comparison, bisect |

## What it scores

Conversation QA across five dimensions: speech, conversation, outcome, policy, reliability. Each scores on its own, then rolls up into one pass/fail verdict.

| Dimension | What it scores |
| :-- | :-- |
| ⏱️ **Speech** | Response latency and turn timing, measured from the two channels. |
| 💬 **Conversation** | Did the agent yield when the caller took the floor, and how fast. |
| 🎯 **Outcome** | Was the job done, judged on tool-call and state evidence. |
| 📋 **Policy** | Required disclosures and PII handling. |
| 📈 **Reliability** | `pass@1` / `pass@k` / `pass^k` with a Wilson interval. |

Timing comes straight from the two channels; say-do reads your `voice_trace.v1` spans, so what the agent told the caller is checked against what the backend did.

## Feed it what you already have

Every onramp feeds the same offline scoring and the same `0` / `1` / `2` exit contract.

**Traces you already log (no audio needed).** Wire the OTel spans you already emit into the same say-do check:

```bash
hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
hotato assert run --trace voice_trace.jsonl --transcript call.transcript.json --assertions assertions.yaml
```

Details: [`docs/ASSERTIONS.md`](https://github.com/attenlabs/hotato/blob/main/docs/ASSERTIONS.md) &#183; [`docs/TRACE.md`](https://github.com/attenlabs/hotato/blob/main/docs/TRACE.md) &#183; ground truth: [`examples/reference-agent`](https://github.com/attenlabs/hotato/blob/main/examples/reference-agent), a 375-run offline suite.

**Your stack's recorded calls.** Vapi, Twilio, and Retell fetch a separated two-channel file; everything scores offline afterwards.

```bash
hotato pull --stack vapi --limit 10
```

Details: [`docs/CONNECT.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONNECT.md) &#183; drive a call against your live agent: [`docs/DRIVE-A-CALL.md`](https://github.com/attenlabs/hotato/blob/main/docs/DRIVE-A-CALL.md)

**Scripted fixtures (no production audio).** A deterministic scripted caller renders a `scenario.v1` labelled `origin=simulated`; a seeded replay is byte-identical.

```bash
hotato simulate --init demo.scenario.json && hotato simulate demo.scenario.json --out ./sim
```

Details: [`docs/SIMULATE.md`](https://github.com/attenlabs/hotato/blob/main/docs/SIMULATE.md)

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo: it reads [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) and runs it end to end, offline, no key.

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
      - uses: attenlabs/hotato@v1.13.0
        with:
          contracts: contracts/          # the catches you committed
          hotato-version: 1.13.0          # exact pin, never a range
```

<details>
<summary><b>Exit-code contract (gate on this, do not parse stdout)</b></summary>

| Exit | Meaning |
| :-: | :-- |
| `0` | every scorable event passed |
| `1` | a scorable event regressed |
| `2` | usage error or unusable input (bad flags, corrupt file, mono recording, or no scorable event) |

Copy-paste workflow with commit-SHA pin: [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md) &#183; [`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md).

</details>

## Drive it over MCP

The MCP server from Quickstart exposes the `voice_eval_run` scorer plus read/verify/propose tools to any MCP client over local stdio. Setup: [`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md).

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

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Provenance and caveats: [`corpus/real/README.md`](https://github.com/attenlabs/hotato/blob/main/corpus/real) &#183; method: [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md) has the details.

</details>

<details>
<summary><b>Two channels, one party each</b></summary>

Timing between two voices is measurable only when they arrive on separate channels; a mono or mixed export is marked **NOT SCORABLE** and refused. It measures timing, not intent: a person labels each candidate moment yield or hold.

```bash
hotato trust --stereo call.wav        # per-channel activity, swap flag, scorability
```

</details>

## Contribute

Issues and PRs are welcome: [`CONTRIBUTING.md`](https://github.com/attenlabs/hotato/blob/main/CONTRIBUTING.md) &#183; [`SECURITY.md`](https://github.com/attenlabs/hotato/blob/main/SECURITY.md) &#183; [`CHANGELOG`](https://github.com/attenlabs/hotato/blob/main/CHANGELOG.md)

**Docs:** [`docs/GETTING-STARTED.md`](https://github.com/attenlabs/hotato/blob/main/docs/GETTING-STARTED.md) &#183; [`AGENTS.md`](https://github.com/attenlabs/hotato/blob/main/AGENTS.md) &#183; [`METHODOLOGY.md`](https://github.com/attenlabs/hotato/blob/main/METHODOLOGY.md) &#183; [`docs/START.md`](https://github.com/attenlabs/hotato/blob/main/docs/START.md) &#183; [`docs/CI.md`](https://github.com/attenlabs/hotato/blob/main/docs/CI.md) &#183; [`docs/CONTRACTS.md`](https://github.com/attenlabs/hotato/blob/main/docs/CONTRACTS.md) &#183; [`docs/MCP.md`](https://github.com/attenlabs/hotato/blob/main/docs/MCP.md)

## License

MIT ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE))

mcp-name: io.github.attenlabs/hotato
