# The hotato lifecycle

Turn production failures into portable tests, run candidate releases against
them, and carry evidence with every release. That is the whole product; this
page maps every command onto it.

The loop has five steps you drive, and one that feeds itself:

```text
OBSERVE -> CATCH -> PIN -> TEST -> PROVE
   ^                                  |
   +---------- production ----------- +
```

Eight nouns carry the loop: a **stack** (the platform your agent runs on), a
**recording** (one two-channel call), a **trace** (the call's pipeline events,
from your OTel spans), a **candidate** (a moment hotato flagged for judgment),
a **label** (your one-word verdict on it), a **contract** (the labeled moment,
packaged as a byte-reproducible check), a **suite** (a named set of tests run
together), and a **proof** (every evidence lane composed into one fail-closed
release verdict).

## 1. Observe

Your calls and traces, on your machine. Nothing leaves it.

| What | Command |
| :-- | :-- |
| Store a stack credential once | `hotato connect --stack vapi` |
| Pull recent recordings in bulk | `hotato pull --stack vapi --since 7d` |
| Fetch and score one call | `hotato capture --stack vapi --call-id ID` |
| Capture OTel spans from any process | `hotato observe capture -- <command>` |
| Ingest an OTel export as a voice trace | `hotato trace ingest --otel spans.json` |
| Run the durable production evidence store | `hotato production serve` |
| Score a webhook's completed call | `hotato ingest --stack vapi --event call.json` |

Cost, latency percentiles, and a self-contained dashboard come straight off the
captured traces: `hotato observe cost`, `hotato observe percentiles`,
`hotato observe report`.

## 2. Catch

Deterministic scoring finds what text evals miss: talk-over, dead air, slow
yields, a tool the agent claimed it ran. Five dimensions (outcome, policy,
conversation, speech, reliability), never one blended score, and every number
reproduces byte for byte.

| What | Command |
| :-- | :-- |
| One recording, ranked candidate moments | `hotato investigate call.wav` |
| Every recent call on a stack, one sweep | `hotato sweep --stack vapi --since 7d` |
| A folder of recordings, ranked dashboard | `hotato analyze recordings/` |
| Is this recording scorable at all | `hotato trust --stereo call.wav` |
| Why a failing event failed, by layer | `hotato explain result.json` |
| Cluster failures across many runs | `hotato diagnose --fleet runs/` |

A transcript works when there is no audio (`hotato investigate --transcript
t.json`), and a chat agent is driven directly (`hotato simulate scenario.json
--chat URL`). The scorer measures timing and say-do, not intent; a mono or
mixed export is refused as NOT SCORABLE, never guessed at.

## 3. Pin

You make the one human decision the loop needs: is this candidate a failure the
agent must never repeat? Your label turns it into a contract: clipped audio,
frame evidence, the policy it was judged under, and a content address, packaged
portable. It re-verifies on any machine, with or without hotato's help.

| What | Command |
| :-- | :-- |
| Label a candidate, mint the contract | `hotato investigate label STATE#1 --expect yield` |
| Pin a moment from a sweep | `hotato fixture promote SWEEP.json#2 --expect hold` |
| File the confirm-or-ignore decision as a GitHub issue | `hotato issue create sweep.json` |
| Build a contract directly from a moment | `hotato contract create call.wav --onset 2.0 --expect yield` |
| Review and label at fleet scale | `hotato fleet review` / `hotato fleet label` |

## 4. Test

Run candidates against everything you have pinned, plus scripted callers,
personas, robustness batteries, and the bundled stress suite. Simulation is
deterministic: the same scenario and seed render the same bytes on every run.

| What | Command |
| :-- | :-- |
| Render a scenario into a scored conversation | `hotato simulate scenario.json --out ./sim` |
| Drive a live call at a candidate agent | `hotato drive contracts/ID.hotato --stack vapi` |
| Stress one recording across noise and loss | `hotato battery robustness call.wav` |
| The bundled turn-taking stress suite | `hotato gauntlet` |
| A suite of conversation tests, per-dimension | `hotato suite run suite.json` |
| Before/after across a whole battery | `hotato verify --before old/ --after new/` |

## 5. Prove

One command composes every evidence lane you have into one fail-closed,
content-addressed proof: contracts re-verified, suites re-run, before/after
movement measured, the stress suite cleared. The proof headlines its claim
scope, exactly what the evidence establishes: contracts alone re-measure stored
evidence (Captured Evidence), a suite or the stress suite establishes a Test
Suite ran, and a before/after run reaches Candidate Revision only when you bind
the candidate identity (`--candidate-config-hash`, `--provider`). CI gates on
the exit code, and the receipt stays verifiable anywhere.

```bash
hotato prove --contracts contracts/ --before before/ --after after/ --out .hotato/proofs/v42/
```

`pass` means every lane passed. Any failure fails the proof; a lane that could
not support its claim is `inconclusive`, and CI never reads "could not tell" as
green.

The Candidate Revision binding is measured, not asserted. `hotato candidate
hash --provider vapi --assistant <id>` fetches the candidate's configuration,
canonicalizes it (dropping volatile fields like timestamps and ids), and prints
its content hash. Re-run `hotato candidate verify --provider vapi --assistant
<id> --expect <hash>` after the before/after calls: it refuses (exit 1) if the
configuration drifted mid-run, so a Candidate Revision proof cannot survive a
swapped candidate. The end to end flow:

```bash
hotato candidate hash --provider vapi --assistant asst_123    # -> sha256:...
hotato drive contracts/ID.hotato --stack vapi --assistant asst_123 --out after/
hotato candidate verify --provider vapi --assistant asst_123 --expect sha256:...  # exit 1 = drifted, void
hotato prove --before before/ --after after/ --candidate-config-hash sha256:... --provider vapi
```

The deeper machinery is there when you need it: `hotato fix trial` binds a
specific patch to its before/after evidence, `hotato apply --clone` stages a
candidate without touching production, and `hotato record render` turns any
failure into a share-safe card.

## ...and production feeds the next loop

The deploy is not the end of the loop; it is the next input. The production
evidence plane watches completed sessions, raises alerts on evidence gaps, and
exports any session as an offline-verifiable regression candidate, which lands
back in step 2.

| What | Command |
| :-- | :-- |
| Continuous evidence intake | `hotato production serve` / `production ingest` |
| Alert transitions on session evidence | `hotato production alerts` |
| A session back into the loop as a test | `hotato production export-regression SESSION` |
| Canary a config change, roll it back | `hotato fleet canary start` / `rollback` |
| Team trends over releases | `hotato team` / `hotato release compare` |

## Where the other commands sit

`hotato start`, `demo`, `doctor`, and `init --auto` are onboarding: they walk
the loop on bundled calls before you spend a minute of your own. `hotato bench`
and `hotato gauntlet badge` prove the scorer itself. `hotato describe` emits
the machine-readable manifest a coding agent drives the loop with, and the MCP
server (`hotato-mcp`) exposes the same loop over stdio.
