<div align="center">

<img src=".github/assets/hotato-banner.svg" alt="hotato" width="442" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=c23c07&label=pypi" alt="PyPI version"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/dm/hotato?style=flat-square&color=d2673a&label=installs%2Fmo" alt="Installs per month"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/attenlabs/hotato/tests.yml?branch=main&style=flat-square&color=2a5f52&label=ci" alt="CI status"></a></p>

### Regression testing for voice agents

</div>

*The transcript passed. The call failed.* Your transcript tests are green, and the call still went wrong: the agent talked over the caller, ran through the interruption, then left a beat of dead air handing the floor back. None of it is in the words. Hotato is self-hosted conversation QA for voice agents: give it a two-channel call recording and it scores the turn timing between caller and agent, verifies what the agent *said* against what the backend *did* from your traces, and rolls outcome, policy, conversation, speech, and reliability into one pass/fail verdict. Every catch locks into a content-addressed contract that returns the same exit `0` or `1` in CI on every machine.

## See the loop catch a regression

One recording in. Hotato ranks the timing moments, hands you the single command to pin the top one, and that pinned failure becomes a CI gate:

```console
$ hotato investigate ./call.wav
hotato investigate [run 1]: call.wav
  input health: eligible for scan
  verdict path: eligible (a labeled event here can carry a real yield/hold verdict)
  most likely failure (top-ranked candidate):
    [1] t=7.63s agent_stop_no_caller  trailing_silence_sec=0.37, caller_proximity_sec=0.5
  next: label it (use --expect hold instead if the agent was right to keep talking):
    hotato investigate label '.hotato/investigate-state.json#1' --expect yield

$ hotato investigate label '.hotato/investigate-state.json#1' --expect yield
created hotato contract: call-8s-yield
  dir:      contracts/call-8s-yield.hotato
  expect:   yield
  passed:   False
  measured: did_yield=False seconds_to_yield=n/a talk_over=0.00s
next:
  hotato contract verify contracts

open the pull request that adds it to your repo's CI gate:
  hotato pr create --fixtures contracts/call-8s-yield.hotato --repo OWNER/REPO --title 'Add hotato contract call-8s-yield'

$ hotato contract verify contracts
hotato contract verify: contracts (1 contract)
  [FAIL] call-8s-yield (expect yield): did_yield=False seconds_to_yield=n/a talk_over=0.00s | integrity: intact
  0/1 contracts pass; exit_code=1
  These contracts pin known failures. Each stays red until you fix the agent and recapture the call, the same way a snapshot test stays red until you update the snapshot.
  Path to green: fix the agent, then recapture with `hotato drive <bundle>` (vapi/twilio), or the manual path in docs/RECAPTURE.md.
```

A committed contract is a pinned bad call: it is *meant* to stay exit `1`. The frozen audio never changes, so the gate goes green only after you fix the agent and recapture. That still-red state is a review checkpoint, not a failing test.

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

Install for daily use with `pipx install hotato` (or `uv tool install hotato`, or `pip install hotato`). Drive it over MCP on local stdio with `uvx --from "hotato[mcp]" hotato-mcp`. New here? Walk the same path with no forks in [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md).

## How it works

```text
two-channel recording
  ->  measure turn timing + verify say-do from the trace
  ->  content-addressed contract
  ->  CI verdict: exit 0 pass / exit 1 fail
```

A catch becomes a contract addressed by its own content, so the exact failure that shipped once reproduces on any machine that runs the suite. Same input, same verdict, every time. Boring on purpose.

## What it scores

Each dimension scores on its own, then rolls up into one pass/fail verdict.

| Dimension | What it scores |
| :-- | :-- |
| ⏱️ **Speech** | Response latency and turn timing, measured from the two channels. |
| 💬 **Conversation** | Did the agent yield when the caller took the floor, and how fast. |
| 🎯 **Outcome** | Was the job done, judged on tool-call and state evidence. |
| 📋 **Policy** | Required disclosures and PII handling. |
| 📈 **Reliability** | `pass@1` / `pass@k` / `pass^k` with a Wilson interval. |

Talk-over, ignored interruptions, and floor-yield latency come straight from the two channels. Say-do verification reads your trace: `tool_call` assertions check the ingested `voice_trace.v1` spans, and `outcome` assertions combine those spans with transcript phrases, so what the agent told the caller is checked against what the backend did.

## Other ways to feed it

Every onramp feeds the same offline scoring and the same `0` / `1` / `2` exit contract. The spine above is the shortest path; these are the others, ordered by friction.

**Traces you already log (no audio needed).** Wire the OTel spans you already emit into the same say-do check:

```bash
hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
hotato assert run --trace voice_trace.jsonl --transcript call.transcript.json --assertions assertions.yaml
```

Details: [`docs/ASSERTIONS.md`](docs/ASSERTIONS.md) &#183; [`docs/TRACE.md`](docs/TRACE.md) &#183; ground truth: [`examples/reference-agent`](examples/reference-agent), a 375-run offline suite (25 scenarios &#215; 5 caller behaviours &#215; 3 audio environments) whose say-do assertions surface four seeded agent defects, deterministically.

**Your stack's recorded calls.** Connect once, then bulk-fetch recent recordings into a local folder. Vapi, Twilio, and Retell fetch a separated two-channel file (Retell by explicit `--call-id`; it has no verified list endpoint). Everything scores offline afterwards; the only network is the recording download.

```bash
hotato pull --stack vapi --limit 10
```

Details: [`docs/CONNECT.md`](docs/CONNECT.md). Once a pulled call shows a catch, [`docs/DRIVE-A-CALL.md`](docs/DRIVE-A-CALL.md) drives one against your live agent.

**Scripted fixtures (no production audio).** A deterministic scripted caller renders a `scenario.v1` into conversation artifacts labelled `origin=simulated`; a seeded replay is byte-identical, so you author regression fixtures without production audio.

```bash
hotato simulate --init demo.scenario.json && hotato simulate demo.scenario.json --out ./sim
```

Details: [`docs/SIMULATE.md`](docs/SIMULATE.md)

## Point your agent at it

Point Claude Code, Cursor, or any coding agent at this repo. It reads [`AGENTS.md`](AGENTS.md) and runs the spine itself: score the demo calls, investigate a recording, label the catch, wire the CI gate. Every step is offline and needs no key.

```text
"Try hotato on the calls in ./recordings and add a CI gate that fails the build on a talk-over regression."
```

## Wire it into CI

The step's exit code **is** hotato's verdict. Drop the Action into a workflow and the build goes red on a regression:

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

The catch you committed once now guards every pull request.

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

The MCP server from Quickstart lets Claude Code, Cursor, or any MCP client score calls, verify contracts, and read verdicts over local stdio. It exposes the `voice_eval_run` scorer plus read/verify/propose tools. Setup: [`docs/MCP.md`](docs/MCP.md).

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

On 13 recorded AMI Meeting Corpus clips, the median error between measured caller-onset and the human word-alignment label is **20 ms**. Output: a per-signal error table and a yield/hold confusion matrix. Provenance (CC BY 4.0 source, sha256-pinned, human word alignments as ground truth) and caveats: [`corpus/real/README.md`](corpus/real). Method: [`METHODOLOGY.md`](METHODOLOGY.md).

</details>

<details>
<summary><b>Two channels, one party each</b></summary>

Timing between two voices is measurable only when they arrive on separate channels. A mono or mixed export can't be split back apart, so hotato marks it **NOT SCORABLE** and refuses. Check scorability first:

```bash
hotato trust --stereo call.wav        # per-channel activity, swap flag, scorability
```

It reads audio energy over time and surfaces candidate moments; a person labels each one yield (should have stopped) or hold (backchannel to talk through). It measures timing, not intent.

</details>

## Contribute

Issues and PRs are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and the [`CHANGELOG`](CHANGELOG.md).

**Docs:** [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) &#183; [`AGENTS.md`](AGENTS.md) &#183; [`METHODOLOGY.md`](METHODOLOGY.md) &#183; [`docs/START.md`](docs/START.md) &#183; [`docs/CI.md`](docs/CI.md) &#183; [`docs/CONTRACTS.md`](docs/CONTRACTS.md) &#183; [`docs/MCP.md`](docs/MCP.md)

## License

MIT ([`LICENSE`](LICENSE))

mcp-name: io.github.attenlabs/hotato
