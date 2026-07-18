# Getting started

One path, no forks: from first touch to a CI gate that guards every pull request.
Follow the five steps in order. Each command prints the exact next command, so
you can chain the whole loop from what hotato tells you.

The transcript passed and the call still failed: the agent talked over the caller,
ran through the interruption, or left a beat of dead air handing the floor back.
None of that is in the words. Hotato scores the turn timing between the two voices
of a recording, pins a caught moment as a contract, and re-runs it in CI forever.

## Install

Zero install, run it now:

```bash
uvx hotato start --demo
```

Keep it around for daily use:

```bash
pipx install hotato        # or: uv tool install hotato, or: pip install hotato
```

Scoring needs two separate channels (caller on one, agent on the other). A mono
or mixed export is marked NOT SCORABLE and refused, exit 2. Confirm a file is
scorable before scoring it with `hotato trust --stereo call.wav`.

## The five steps

### 1. See it catch a failure

```bash
hotato start --demo
```

Sweeps the two bundled demo calls, builds one failure contract, and runs one
say-do conversation check. It exits 0 because setup finished. The gate command it
points at, `hotato contract verify contracts/`, exits 1 by design.

### 2. Score your own recording

```bash
hotato investigate ./call.wav
```

```console
hotato investigate [run 1]: call.wav
  input health: eligible for scan
  verdict path: eligible (a labeled event here can carry a real yield/hold verdict)
  most likely failure (top-ranked candidate):
    [1] t=7.63s agent_stop_no_caller  trailing_silence_sec=0.37, caller_proximity_sec=0.5
  next: label it (use --expect hold instead if the agent was right to keep talking):
    hotato investigate label '.hotato/investigate-state.json#1' --expect yield
```

Hotato ranks the timing moments, marks the top one `most likely failure`, and
hands you one command to pin it. It infers no intent. Have a provider call id
instead of a WAV? `hotato investigate --stack vapi --call-id <id>` pulls it first,
then ranks the same way.

### 3. Commit the catch as a regression

Yield means the agent should have stopped for the caller. Hold means it should
have kept the floor through a backchannel or noise. The label is your decision;
hotato measures whether the timing matched it.

```bash
hotato investigate label '.hotato/investigate-state.json#1' --expect yield
```

```console
created hotato contract: call-8s-yield
  dir:      contracts/call-8s-yield.hotato
  expect:   yield
  passed:   False
  measured: did_yield=False seconds_to_yield=n/a talk_over=0.00s
next:
  hotato contract verify contracts

open the pull request that adds it to your repo's CI gate:
  hotato pr create --fixtures contracts/call-8s-yield.hotato --repo OWNER/REPO --title 'Add hotato contract call-8s-yield'
```

The contract bundle is content-addressed: the clipped audio, the frame-level
evidence, the policy, and its own manifest, committed byte-identical.

### 4. Open the pull request

```bash
hotato pr create --fixtures contracts/call-8s-yield.hotato --repo OWNER/REPO --title 'Add hotato contract call-8s-yield'
```

Stages the bundle byte-identical under `tests/hotato/contracts/` and opens the
PR. It is a dry run by default; add `--yes` to run git and gh.

### 5. Let the gate re-run the evidence

```bash
hotato contract verify contracts/
```

```console
hotato contract verify: contracts (1 contract)
  [FAIL] call-8s-yield (expect yield): did_yield=False seconds_to_yield=n/a talk_over=0.00s | integrity: intact
  0/1 contracts pass; exit_code=1
  These contracts pin known failures. Each stays red until you fix the agent and recapture the call, the same way a snapshot test stays red until you update the snapshot.
  Path to green: fix the agent, then recapture with `hotato drive <bundle>` (vapi/twilio), or the manual path in docs/RECAPTURE.md.
```

This re-measures the stored evidence deterministically. It is the CI gate: exit 0
pass, exit 1 fail.

## When the gate is red

A committed contract is a pinned bad call, so it is *meant* to stay exit 1, the
way a snapshot test stays red until you update the snapshot. The frozen audio
never changes, so the gate goes green only after you fix the agent and recapture
the call. That still-red state is a review checkpoint, not a broken test.

To get to green, fix the agent, then recapture:

```bash
hotato drive <bundle>        # re-run a fresh call against the live agent (vapi/twilio)
```

`hotato drive` originates a new call for vapi and twilio and reports a before and
after verdict. For every other stack, or by hand, follow [`RECAPTURE.md`](RECAPTURE.md).

## Reference

The five steps are the whole loop. When you need more depth, follow these one at
a time; none of them is required to complete the loop above.

- [`START.md`](START.md): the guided demo, both acts (timing and say-do), in detail.
- [`INVESTIGATE.md`](INVESTIGATE.md): capture-origin authentication and candidate ranking.
- [`CONTRACTS.md`](CONTRACTS.md): what a contract bundle holds and what `verify` proves.
- [`CI.md`](CI.md): the copy-paste GitHub Action with a commit-SHA pin and a PR results comment.
- [`RECAPTURE.md`](RECAPTURE.md): the path from a red gate to a green one.
- [`ASSERTIONS.md`](ASSERTIONS.md) and [`TRACE.md`](TRACE.md): the say-do check from traces you already log.
- [`MCP.md`](MCP.md): drive the loop from Claude Code, Cursor, or any MCP client.
- [`../AGENTS.md`](../AGENTS.md): the same loop, written for a coding agent, plus the machine contract.

Symptom-first walkthroughs, each opening with the direct answer and running on
what ships in the repo:

- [`scenarios/browser-vs-pstn.md`](scenarios/browser-vs-pstn.md): passes in the browser, fails on the phone; score the same moment clean and codec-degraded.
- [`latency-waterfall.md`](latency-waterfall.md): perceived latency worse than the dashboard; the per-hop waterfall from your traces.
- [`scenarios/load-and-recovery.md`](scenarios/load-and-recovery.md): breaks under concurrent call load; the evidence-preserving load staircase.
- [`scenarios/dtmf-verification.md`](scenarios/dtmf-verification.md): verifying DTMF from the evidence your pipeline logs.
- [`scenarios/echo-self-interruption.md`](scenarios/echo-self-interruption.md): the agent interrupts itself; TTS bleed measured and gated.
- [`scenarios/false-interruption-replay.md`](scenarios/false-interruption-replay.md): stops talking on "mhm"; pin the false yield as a CI contract.
