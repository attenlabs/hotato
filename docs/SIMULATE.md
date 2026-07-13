# Simulate (`hotato simulate`): a scenario to a deterministic, labelled conversation

`hotato simulate` renders a **scenario** (`hotato.scenario.v1`) through a
DETERMINISTIC scripted caller into one or more **conversation artifacts**
(`hotato.conversation.v1`), each labelled `origin=simulated`. It runs fully
offline, scripted-caller only: the deterministic INPUT side of a simulation --
the ground-truth a caller holds, the caller's scripted turns, the environment,
and an optional variation matrix.

It is the trustworthy regression foundation you build BEFORE any generative
caller: a fixed `(scenario, seed)` reproduces the transcript **byte-for-byte**
(the produced transcript is content-hashed), so a run is a stable, reusable
fixture.

## Quickstart (works from a bare `pip install`)

```bash
pip install hotato

# 1. Write a minimal scenario simulate accepts as-is:
hotato simulate --init demo.scenario.json

# 2. Render it into a labelled origin=simulated conversation:
hotato simulate demo.scenario.json --out ./sim

# 3. (optional) digest-verify the produced artifact:
hotato conversation verify ./sim
```

Prefer zero files? The package ships a minimal scenario you can run directly --
no file on disk:

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

The `./sim` directory holds a `conversation.json` plus its bound transcript and
`voice_trace.jsonl`, all `origin.kind=simulated`.

> **Two different `scenario` concepts, one name.** `hotato simulate` consumes a
> `hotato.scenario.v1` doc -- author one with `hotato simulate --init`.
> `hotato scenario init` writes a separate file, a `hotato.conversation-test.v1`
> that `hotato test run` consumes -- see
> [CONVERSATION-TEST.md](CONVERSATION-TEST.md). Feed a conversation-test file
> to `simulate` and you get an actionable error that points you back to
> `--init`.

## What `--init` writes

`hotato simulate --init demo.scenario.json` writes a MINIMAL, valid
`hotato.scenario.v1` doc. It is a starter you edit for your own agent -- shape
the caller turns to match your own call. The scenario id is derived from the
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

The caller's script holds only the caller's own turns -- a `say` is the caller
speaking. The schema enforces this structurally: there's no field for the
agent's words, so a scenario stays scoped to the caller's side by construction,
at the schema level.

Required fields: `kind`, `version`, `id`, `goal` (`type` + `target`), and
`caller.script` (at least one `say`). The full schema, including `variation_matrix`
and the optional deterministic `agent_mock`, is
[`schema/scenario.v1.json`](../src/hotato/schema/scenario.v1.json).

## The invariants (enforced structurally)

- **`origin=simulated` on every produced conversation**, kept in its own
  bucket, apart from real calls. `write_artifact` only writes artifacts whose
  origin is simulated.
- **A bad rendering is `SIMULATOR_INVALID`, kept distinct from an agent
  PASS/FAIL.** The simulator only decides whether the produced conversation is
  a FAITHFUL rendering of its scenario. Scoring is the SEPARATE assert layer's
  job, over the produced artifact.
- **Reproducibility means a SEEDED REPLAY is byte-identical** -- there's no
  model in this path. A fixed `(scenario, seed)` produces the same transcript
  bytes every time; different seeds differ ONLY where the scenario allows it
  (probabilistic backchannels).
- **Each dimension scored on its own lane** -- enforced structurally in both
  the schema, which rejects an `overall_score` key, and the code path.

## Reliability: pass@1 / pass@k / pass^k

`simulate` reports **Reliability** as its own dimension, scored on its own lane:

- `pass@1` -- the fraction of runs that rendered faithfully.
- `pass@k` -- at-least-one-passes across `k` runs.
- `pass^k` -- all-`k`-pass.

For the scripted deterministic caller `pass^k == pass@1`: a seeded replay is
byte-identical, so every run has the same outcome. That is the correct,
plainly reported outcome; variance shows up only where the scenario
introduces it. `--repetitions N` expands the variation matrix so Reliability
is measured over `N` runs.

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

The summary stays byte-identical no matter the worker count. Every result is
attributed to its own variation cell, each scored on its own lane.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | every produced conversation is `origin=simulated` and validated as a faithful rendering (and, under `--matrix --conversation-test`, every scored aggregate passed). |
| `1`  | at least one produced simulation was `SIMULATOR_INVALID` -- a broken fixture, kept distinct from an agent PASS/FAIL -- or, under `--matrix --conversation-test`, a scored aggregate FAILed. |
| `2`  | usage error / unusable input: a malformed or unreadable scenario / conversation-test file (or, under `--matrix --conversation-test` with `inconclusive_policy refuse`, a withheld verdict). |

## Related

- [CONVERSATION-TEST.md](CONVERSATION-TEST.md) -- `hotato scenario init` /
  `hotato test run`: score a real call against a conversation-test.
- [`schema/scenario.v1.json`](../src/hotato/schema/scenario.v1.json) -- the full
  scenario schema.
