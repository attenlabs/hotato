# hotato persona pack: `hotato-voice-personas`

A curated, seeded persona and scenario library for `hotato simulate`. Each
entry is a `hotato.scenario.v1` document that renders through the deterministic
scripted caller into a labelled `origin=simulated` conversation.

## What it gives you

Seven common voice-agent test cases, ready to run with no file authoring:

| Name | Tests |
|---|---|
| `barge-in-missed` | The caller takes the floor mid-sentence; see whether your agent stops and listens. |
| `backchannel-not-floor-take` | The caller drops short acknowledgements while listening; see whether your agent holds the floor. |
| `dead-air-silence` | The caller goes quiet and re-prompts; see whether your agent recovers a stalled call. |
| `over-eager-early-response` | The caller pauses mid-request; see whether your agent waits for the caller to finish. |
| `caller-talk-over` | The caller overlaps the agent twice; see whether your agent yields under sustained talk-over. |
| `fast-interrupter` | A fast-paced caller cuts in early; a pacing/temperament variation. |
| `slow-speaker` | A slow-paced caller spreads one request across long turns; a pacing/temperament variation. |

## Reproducibility

Every entry pins a fixed `seed`. A fixed `(scenario, seed)` renders
byte-identical every run -- `conversation.json`, `transcript.json`, and
`trace.jsonl` all match -- so the same test case is a stable, reusable fixture
across machines and CI runs. The manifest `created_at` defaults to a reproducible
instant (SOURCE_DATE_EPOCH-style, never the wall clock); pass `--created-at` or
set `$SOURCE_DATE_EPOCH` to pin a real timestamp. `manifest.json` is the
machine-readable index the CLI reads for `hotato simulate --list`.

## Run it

```bash
# list the pack
hotato simulate --list

# render one entry into a labelled origin=simulated conversation
hotato simulate barge-in-missed --out ./sim
```

## Scope

Each entry renders the caller side of a scenario into an `origin=simulated`
conversation, labelled as such. It is scripted-caller stimulus, not production
audio, and the pack scores nothing on its own: scoring is the separate assert
layer's job over the produced artifact. The caller declares only its own turns.
See [`docs/SIMULATE.md`](../../../../../docs/SIMULATE.md).
