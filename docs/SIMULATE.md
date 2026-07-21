# `hotato simulate`: a scenario to a deterministic, labelled conversation

`hotato simulate` renders a **scenario** (`hotato.scenario.v1`) through a
deterministic scripted caller into one or more **conversation artifacts**
(`hotato.conversation.v1`), each labelled `origin=simulated`. Fully
offline, scripted-caller only -- the deterministic input side of a
simulation: the ground truth a caller holds, the caller's scripted turns,
the environment, and an optional variation matrix.

Use it to build the regression foundation you want before any generative
caller: a fixed `(scenario, seed)` reproduces the transcript byte-for-byte
(content-hashed), so a run is a stable, reusable fixture.

## Quickstart (zero-install with uvx)

```bash
# run zero-install with uvx (or: pipx install hotato)
uvx hotato --help

# 1. Write a minimal scenario simulate accepts as-is:
hotato simulate --init demo.scenario.json

# 2. Render it into a labelled origin=simulated conversation:
hotato simulate demo.scenario.json --out ./sim

# 3. (optional) digest-verify the produced artifact:
hotato conversation verify ./sim
```

Prefer zero files? The package ships a minimal scenario you can run
directly, no file on disk:

```bash
hotato simulate --example --out ./sim
```

Step 2 prints:

```
wrote 1 simulated artifact(s) under ./sim/
hotato simulate: demo -- 1 run(s), origin=simulated (never real)
  run 1: seed=0 77f1eb9e6994 sim=ok  -> ./sim
reliability: pass@1=1.000 pass@k=1.000 pass^k=1.000 (n=1)
```

`./sim` holds a `conversation.json` plus its bound transcript and
`voice_trace.jsonl`, all `origin.kind=simulated`.

## The curated persona pack (seeded, byte-reproducible)

The package ships a curated persona/scenario library, `hotato-voice-personas`,
covering the common voice-agent test cases: a missed barge-in, a backchannel
that is not a floor-take, long silence / dead air, a mid-utterance pause before
an over-eager reply, a caller talking over the agent, and pacing variations (a
fast interrupter, a slow speaker). Each entry is a real `hotato.scenario.v1`
the deterministic caller renders, and each pins a fixed `seed`, so a fixed
`(scenario, seed)` renders **byte-identical every run** across machines and CI:
`conversation.json`, `transcript.json`, and `trace.jsonl` all match, because the
manifest `created_at` defaults to a reproducible instant (SOURCE_DATE_EPOCH-style,
never the wall clock). Pass `--created-at` (or set `$SOURCE_DATE_EPOCH`) to pin a
real timestamp when you want one. Run a common test case by name, no file
authoring:

```bash
# list the pack (add --format json for the machine-readable index):
hotato simulate --list

# run one pack scenario by name into a labelled origin=simulated conversation:
hotato simulate barge-in-missed --out ./sim
```

`hotato simulate --list` prints:

```
hotato simulate pack: hotato-voice-personas -- 7 curated scenario(s)
each renders a deterministic origin=simulated caller (never real); a fixed (scenario, seed) is byte-identical
  backchannel-not-floor-take  [should_hold] seed=202  Backchannel that is not a floor-take
      The caller drops short acknowledgements (mm-hmm, right) while listening. Run it to see whether your agent holds the floor instead of treating a backchannel as an interruption.
  barge-in-missed             [should_yield] seed=101  Missed barge-in (hard interruption)
      The caller takes the floor mid-sentence at a fixed offset. Run it to see whether your agent stops its own speech and listens.
  ...
run one with: hotato simulate <name> --out ./sim
```

The name resolves to a scenario bundled with the package; a scenario file on
disk always wins, so a local file is never shadowed by a pack name. A pack
scenario is deterministic INPUT that renders `origin=simulated`, labelled as
such; it is scripted-caller stimulus, not production audio, and it scores
nothing on its own -- scoring is the separate assert layer's job over the
produced artifact. The pack lives in
[`src/hotato/data/simulate/pack/`](../src/hotato/data/simulate/pack/) with its
own `README.md` and `manifest.json`.

> **Two different `scenario` concepts, one name.** `hotato simulate`
> consumes a `hotato.scenario.v1` doc -- author one with `hotato simulate
> --init`. `hotato scenario init` writes a separate file, a
> `hotato.conversation-test.v1`, that `hotato test run` consumes -- see
> [CONVERSATION-TEST.md](CONVERSATION-TEST.md). Feed a conversation-test
> file to `simulate` and you get an actionable error pointing back to
> `--init`.

## What `--init` writes

`hotato simulate --init demo.scenario.json` writes a minimal, valid
`hotato.scenario.v1` doc -- a starter you edit for your own agent, shaping
the caller turns to match your call. The scenario id derives from the
filename stem.

```json
{
  "kind": "hotato.scenario",
  "version": 1,
  "id": "demo",
  "goal": { "type": "get_refund", "target": "order A-1001" },
  "facts": { "order_id": "A-1001" },
  "caller": {
    "script": [
      { "say": "Hi, my order A-1001 arrived damaged and I would like a refund." },
      { "say": "Yes, please refund it to my card." }
    ],
    "behavior": { "backchannels": { "probability": 0.0 } }
  },
  "environment": { "locale": "en-US", "route": "phone" },
  "seed": 0
}
```

The caller's script holds only the caller's own turns -- a `say` is the
caller speaking. The schema enforces this structurally: there's no field
for the agent's words, so a scenario stays scoped to the caller's side by
construction.

Required fields: `kind`, `version`, `id`, `goal` (`type` + `target`), and
`caller.script` (at least one `say`). The full schema, including
`variation_matrix` and the optional deterministic `agent_mock`, is
[`schema/scenario.v1.json`](../src/hotato/schema/scenario.v1.json).

## The invariants, enforced structurally

- **`origin=simulated` on every produced conversation**, kept apart from
  real calls. `write_artifact` only writes artifacts whose origin is
  simulated.
- **A bad rendering is `SIMULATOR_INVALID`, kept distinct from an agent
  PASS/FAIL.** The simulator only decides whether the produced
  conversation faithfully renders its scenario; scoring is the separate
  assert layer's job.
- **A seeded replay is byte-identical** -- there's no model in this path.
  A fixed `(scenario, seed)` produces the same transcript bytes every
  time; different seeds differ only where the scenario allows it
  (probabilistic backchannels).
- **Each dimension scores on its own lane**, enforced in both the schema
  (which rejects an `overall_score` key) and the code path.

## Reliability: pass@1 / pass@k / pass^k

`simulate` reports Reliability as its own dimension, scored on its own
lane:

- `pass@1` -- the fraction of runs that rendered faithfully.
- `pass@k` -- at least one pass across `k` runs.
- `pass^k` -- all `k` pass.

For the scripted deterministic caller, `pass^k == pass@1`: a seeded replay
is byte-identical, so every run has the same outcome. Variance shows up
only where the scenario introduces it. `--repetitions N` expands the
variation matrix so Reliability is measured over `N` runs.

## Simulate many scenarios in parallel (`--matrix`)

```bash
# expand the scenario's FULL variation matrix (locale x speaking_rate x noise x
# behavior x repetitions), render + validate each in a bounded pool:
hotato simulate --matrix demo.scenario.json --out ./matrix

# score each produced conversation against a conversation-test's DETERMINISTIC
# assertions; SIMULATOR_INVALID runs are bucketed separately, kept distinct
# from a PASS/FAIL:
hotato simulate --matrix demo.scenario.json \
    --conversation-test refund.test.yaml --parallel 8 --format json
```

The summary stays byte-identical no matter the worker count. Every result
is attributed to its own variation cell, each scored on its own lane.

## Drive the scripted caller at YOUR chat agent (`--chat URL`)

`--chat` drives the same scripted caller turn plan against your own chat
agent over HTTP and writes a timestamped transcript
[`hotato investigate --transcript`](INVESTIGATE.md) scores:

```bash
hotato simulate demo.scenario.json --chat http://127.0.0.1:8080/chat
hotato investigate --transcript .hotato/chat-transcript.json
```

The whole wire contract is one POST per scripted turn:

```
request:   POST <URL>   {"conversation_id": "<id>", "turn_index": 0, "text": "<caller turn>"}
response:  200          {"text": "<agent reply>"}
```

Extra response keys are ignored; a non-200, a redirect, a non-JSON body, or
a missing `text` is a loud exit-2 error naming the contract. Local by
default: a host off `localhost`/`127.0.0.1` is refused before any request
unless you pass `--egress-opt-in` (the same explicit gate the hosted
diarizer and hosted judge carry).

What lands in the transcript, each labelled for what it is:

- agent replies **verbatim**, with per-turn reply latency **measured** as the
  HTTP round trip -- that measured latency is exactly the response gap
  `investigate --transcript` scores;
- caller/agent turn spans from the scenario's deterministic pacing model
  (the same word-count/speaking-rate constants the offline renderer uses),
  so the caller side derives from `(scenario, seed)`, never a wall clock;
- `origin.kind = "simulated"` provenance: the caller side is the scripted
  simulator's, and the agent text is your agent's own replies
  (`agent_replies: "live-chat-http"`).

`--out DIR` names the transcript's directory (default `.hotato/`, written
as `chat-transcript.json`).

## Exit codes

| Exit | Meaning |
|---|---|
| `0` | every produced conversation is `origin=simulated`, validated as a faithful rendering (and, under `--matrix --conversation-test`, every scored aggregate passed); with `--chat`, every scripted turn was driven and the transcript written |
| `1` | at least one simulation was `SIMULATOR_INVALID` -- a broken fixture, distinct from an agent PASS/FAIL -- or, under `--matrix --conversation-test`, a scored aggregate FAILed |
| `2` | usage error / unusable input: a malformed or unreadable scenario/conversation-test file (or, under `--matrix --conversation-test` with `inconclusive_policy refuse`, a withheld verdict); a non-local `--chat` URL without `--egress-opt-in`, an unreachable chat agent, or a reply off the `--chat` contract |

## Related

- [CONVERSATION-TEST.md](CONVERSATION-TEST.md) -- `hotato scenario init` /
  `hotato test run`: score a real call against a conversation-test.
- [`schema/scenario.v1.json`](../src/hotato/schema/scenario.v1.json) -- the
  full scenario schema.
