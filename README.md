<div align="center">

<img src=".github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/dm/hotato?style=flat-square&color=d2673a&label=installs%2Fmo" alt="Installs per month"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/attenlabs/hotato/tests.yml?branch=main&style=flat-square&color=2a5f52&label=ci" alt="CI status"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6f5d44?style=flat-square" alt="License: MIT"></a>
<br>
<a href="docs/MCP.md"><img src="https://img.shields.io/badge/MCP-ready-c23c07?style=flat-square" alt="MCP ready"></a>
<img src="https://img.shields.io/badge/offline-by%20default-2a5f52?style=flat-square" alt="Offline by default">
<a href="https://github.com/attenlabs/hotato/attestations"><img src="https://img.shields.io/badge/build-provenance%20attested-2a5f52?style=flat-square" alt="Build provenance attested"></a>
<img src="https://img.shields.io/badge/installed-~10%20MiB-6f5d44?style=flat-square" alt="Installed footprint ~10 MiB"></p>

### Regression testing for voice agents

<p align="center">
<a href="#quickstart"><b>Quickstart</b></a> &#183;
<a href="#three-ways-in">Three ways in</a> &#183;
<a href="#point-your-agent-at-it">Point an agent at it</a> &#183;
<a href="#how-it-works">How it works</a> &#183;
<a href="#five-dimensions-one-verdict">Five dimensions</a> &#183;
<a href="#wire-it-into-ci">CI gate</a> &#183;
<a href="#drive-it-over-mcp">MCP</a>
</p>

</div>

Hotato is self-hosted regression testing for voice agents: give it a two-channel call recording, it scores the turn timing between caller and agent, and it returns the same exit `0` or `1` verdict in CI on every machine. It also verifies what the agent *said* against what the backend *did* from your traces, and locks every catch into a content-addressed failure record that reproduces byte-for-byte.

*The transcript passed. The call failed.* Your transcript tests are green, and the call still went wrong: the agent talked over the caller, ran straight through the interruption, and took a beat too long to hand the floor back. None of it is in the words. **hotato** gives that failure a number, then locks each catch into a CI contract.

## Key properties

- 📐 Conversation QA for voice agents: scores five dimensions (outcome, policy, conversation, speech, reliability) into one pass/fail verdict.
- ⏱️ Scores talk-over, ignored interruptions, and floor-yield latency, measured from two channels.
- 🔒 Each catch is a content-addressed contract that reproduces byte-for-byte across machines and releases.
- 🪶 Stdlib-only core: zero required dependencies, ~10 MiB installed, no network calls.
- 🤖 Reads [`AGENTS.md`](AGENTS.md); a coding agent runs the whole loop.
- 🌐 MIT-licensed, self-hosted, off the production audio path.

## Quickstart

Zero setup. Scores the bundled demo calls and prints each caught moment, credential-less.

```bash
uvx hotato start --demo                # scores bundled recorded calls, no account
```

That sweeps the two bundled demo calls, scores the timing between the voices, and exits `1` on the one where the agent ran through the caller.

Keep it in a project, or drive it over MCP on local stdio:

```bash
pipx install hotato                    # add it to your repo
uvx --from "hotato[mcp]" hotato-mcp    # drive it over MCP, local stdio
```

## Three ways in

Ordered by friction. Start with the data you already have; every path feeds the same offline scoring and the same exit-code gate.

**1. Traces you already log (no audio needed).** `tool_call` assertions read only the ingested trace's `voice_trace.v1` spans; `outcome` assertions combine those spans with transcript phrases: say-do verification that what the agent told the caller matches what the backend did, deterministic end to end.

```yaml
# assertions.yaml
version: 1
assertions:
  - id: refund-was-issued
    kind: tool_call
    name: issue_refund
  - id: said-and-did
    kind: outcome
    all_of: [{tool_called: issue_refund}, {phrase: "refund is on its way", role: agent}]
```

```bash
hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
hotato assert run --trace voice_trace.jsonl --transcript call.transcript.json --assertions assertions.yaml
```

Details: [`docs/ASSERTIONS.md`](docs/ASSERTIONS.md) &#183; [`docs/TRACE.md`](docs/TRACE.md) &#183; ground truth: [`examples/reference-agent`](examples/reference-agent), a 375-run offline suite (25 scenarios &#215; 5 caller behaviours &#215; 3 audio environments) whose say-do assertions surface four seeded agent defects, deterministically.

**2. Your stack's recorded calls.** Connect once, then bulk-fetch recent recordings into a local folder. Vapi, Twilio, and Retell fetch a separated two-channel file (Retell by explicit `--call-id`; it has no verified list endpoint). Everything scores offline afterwards; the only network is the recording download.

```bash
hotato pull --stack vapi --limit 10
```

Details: [`docs/CONNECT.md`](docs/CONNECT.md). Once a pulled call shows a catch, the second move is driving one against your live agent: [`docs/DRIVE-A-CALL.md`](docs/DRIVE-A-CALL.md).

**3. Scripted fixtures (no production audio).** A deterministic scripted caller renders a `scenario.v1` into conversation artifacts labelled `origin=simulated`; a seeded replay is byte-identical, so you author regression fixtures without production audio.

```bash
hotato simulate --init demo.scenario.json && hotato simulate demo.scenario.json --out ./sim
```

Details: [`docs/SIMULATE.md`](docs/SIMULATE.md)

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo. It reads [`AGENTS.md`](AGENTS.md) and runs the loop itself: score the demo calls, ingest a recording, wire a CI gate, re-check the numbers. Every step is offline and needs no key.

```text
"Try hotato on the calls in ./recordings and add a CI gate that fails the build on a talk-over regression."
```

## Capabilities

<table>
<tr>
<td width="50%" valign="top">

⏱️ **Timing measurement**<br/>
Talk-over, ignored interruptions, and floor-yield latency, measured from the two channels.

</td>
<td width="50%" valign="top">

🎯 **Five scored dimensions**<br/>
Outcome, policy, conversation, speech, and reliability roll up into one pass/fail verdict.

</td>
</tr>
<tr>
<td width="50%" valign="top">

🧾 **Say-do verification**<br/>
`tool_call` assertions read only the ingested trace's spans; `outcome` combines them with transcript phrases into one say-do check.

</td>
<td width="50%" valign="top">

🗂️ **Committable evidence**<br/>
Each catch saves as a contract bundle you commit, diff, and review with code.

</td>
</tr>
<tr>
<td width="50%" valign="top">

🔌 **Importers**<br/>
Ingest the call exports your stack already produces from Vapi, Retell, Twilio.

</td>
<td width="50%" valign="top">

🧪 **CI gate**<br/>
Drop the Action into a workflow; the step's exit code is hotato's verdict.

</td>
</tr>
<tr>
<td width="50%" valign="top">

🎭 **Scripted simulation**<br/>
A deterministic scripted caller authors `origin=simulated` fixtures; a seeded replay is byte-identical.

</td>
<td width="50%" valign="top">

🤖 **Agent surfaces**<br/>
An agent drives hotato from [`AGENTS.md`](AGENTS.md) and `hotato describe --format json`.

</td>
</tr>
<tr>
<td width="50%" valign="top">

🧩 **MCP-ready**<br/>
Score calls, verify contracts, and read verdicts over local stdio from any MCP client.

</td>
<td width="50%" valign="top">

🛰️ **Self-hosted**<br/>
Credential-less; runs on the machine that invokes it.

</td>
</tr>
</table>

## How it works

```mermaid
flowchart TD
  A[Two-channel recording] --> B[Measure the timing between the two voices]
  B --> C[Content-addressed contract]
  C --> D{CI verdict}
  D -->|exit 0| E[pass]
  D -->|exit 1| F[fail]
```

A catch becomes a contract addressed by its own content, so the exact failure that shipped once reproduces on any machine that runs the suite. Same input, same verdict, every time.

## Five dimensions, one verdict

| Dimension | What it scores |
| :-- | :-- |
| 🎯 **Outcome** | Was the job done, judged on tool-call and state evidence. |
| 📋 **Policy** | Required disclosures and PII handling. |
| 💬 **Conversation** | Did the agent yield when the caller took the floor, and how fast. |
| 🗣️ **Speech** | Response latency and turn timing. |
| 📈 **Reliability** | `pass@1` / `pass@k` / `pass^k` with a Wilson interval. |

> **Two channels, one party each.** A mono or bad export is marked **NOT SCORABLE**, so a verdict measures timing, not intent.

## See a scored call

<p align="center">
<img src="docs/assets/hotato-cast.gif" alt="hotato demo: scoring a recorded call and showing the report" width="820"><br/>
<sub>Scoring a real recorded call: the exact command and hotato's real scorecard.</sub>
</p>

## Specifications

| Property | Value |
| :-- | :-- |
| Footprint | ~10 MiB installed |
| Core dependencies | 0 (stdlib-only) |
| Reproducibility | byte-for-byte, content-addressed contract |
| Exit contract | `0` pass · `1` fail · `2` refuse |
| Release integrity | OIDC Trusted Publishing + build-provenance attested |
| Runtime | offline, off the production audio path |

## Wire it into CI

The step's exit code **is** hotato's exit code: `0` pass, `1` fail, `2` refuse. Drop the Action into a workflow and the build goes red on a regression:

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
      - uses: attenlabs/hotato@v1.8.1
        with:
          contracts: contracts/          # the catches you committed
          hotato-version: 1.8.1          # exact pin, never a range
```

The catch you committed once now guards every pull request and reproduces the same verdict on the reviewer's machine.

<details>
<summary><b>Exit-code contract (gate on this, do not parse stdout)</b></summary>

<br>

| Exit | Meaning |
| :-: | :-- |
| `0` | every scorable event passed |
| `1` | a scorable event regressed |
| `2` | usage error or unusable input (bad flags, corrupt file, mono recording, or no scorable event) |

Copy-paste workflow with commit-SHA pin: [`docs/CI.md`](docs/CI.md) &#183; [`docs/CONTRACTS.md`](docs/CONTRACTS.md).

</details>

## Drive it over MCP

```bash
uvx --from "hotato[mcp]" hotato-mcp     # local stdio, no key
```

Point Claude Code, Cursor, or any MCP client at it to score calls, verify contracts, and read verdicts over the protocol. It exposes the `voice_eval_run` scorer plus read/verify/propose tools. Setup: [`docs/MCP.md`](docs/MCP.md).

## Verify the measurement yourself

<details>
<summary><b>Re-run the measurement benchmark</b></summary>

<br>

```bash
# re-run the measurement-error benchmark on the recorded AMI clips
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Output: a per-signal error table and a yield/hold confusion matrix. Provenance (CC BY 4.0 source, sha256-pinned, human word alignments as ground truth) and caveats: [`corpus/real/README.md`](corpus/real). Method: [`METHODOLOGY.md`](METHODOLOGY.md).

</details>

<details>
<summary><b>Two channels, one party each</b></summary>

<br>

Timing between two voices is measurable only when they arrive on separate channels. A mono or mixed export can't be split back apart, so hotato marks it **NOT SCORABLE** and refuses. Check scorability first:

```bash
hotato trust --stereo call.wav        # per-channel activity, swap flag, scorability
```

It reads audio energy over time and surfaces candidate moments; a person labels each one yield (should have stopped) or hold (backchannel to talk through). It measures timing, not intent.

</details>

## Contribute

Issues and PRs are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and the [`CHANGELOG`](CHANGELOG.md).

**Docs:** [`AGENTS.md`](AGENTS.md) &#183; [`METHODOLOGY.md`](METHODOLOGY.md) &#183; [`docs/START.md`](docs/START.md) &#183; [`docs/CI.md`](docs/CI.md) &#183; [`docs/CONTRACTS.md`](docs/CONTRACTS.md) &#183; [`docs/MCP.md`](docs/MCP.md)

## License

MIT ([`LICENSE`](LICENSE))

mcp-name: io.github.attenlabs/hotato
