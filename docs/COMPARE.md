# Compare: hotato vs hosted platforms

Local-first testing and observability for AI agents, next to the hosted
platforms that run the same jobs as a service. The short version: the same
lifecycle, three structural differences.

## The lifecycle, side by side

Every step below runs on your machine, from the CLI, with an exit code CI can
gate on. The right column is what the equivalent step costs on a hosted
platform, structurally, whoever the vendor is.

| Lifecycle step | hotato | Hosted platforms |
|---|---|---|
| **Observe** traces, cost, latency | `hotato observe` on the OTel spans you already emit, locally | your traces live on their servers |
| **Catch** the failures evals miss | `hotato investigate` / `sweep`: deterministic timing and say-do scoring | model-scored, so the number drifts run to run |
| **Pin** a failure as a test | `hotato investigate label`: a portable, content-addressed contract | a dataset row inside their account |
| **Test** candidates against it | `hotato simulate` / `drive` / `gauntlet`, seeded and byte-reproducible | simulation credits, metered per run |
| **Prove** a release | `hotato prove`: every lane composed into one fail-closed receipt | a dashboard score you cannot re-derive |
| **Confirm** in production | `hotato production` alerts, exported back into the loop as tests | alerts inside their service |

## The three structural differences

- **Price at scale.** Free and MIT at any volume. There is no per-seat, per-run,
  or per-event meter anywhere in the loop.
- **Verdicts you can gate on.** The same input scores the same way on every
  machine, byte for byte, so an exit code can block a merge. A judged score
  that varies run to run cannot.
- **Your data stays yours.** Recordings, traces, prompts, and backend state
  never leave your machine, and a contract or proof stays verifiable if you
  stop using hotato, if the vendor changes, or if the service is down.

## The layer hotato is not

A runtime voice layer (an orchestration platform's endpointing, a turn
detector, barge-in suppression) acts *during* the call. hotato runs *after*,
from the recording and the trace: it measures what happened, pins what must not
happen again, and proves a candidate against it. Run the runtime layer that
makes your median call good; run hotato so the failure a caller hit last week
stays fixed on every push. The frozen moment a runtime layer got wrong is
exactly what `hotato investigate label` pins and `hotato prove` re-verifies.

## What the scoring claims, precisely

- Timing and say-do, not intent: energy over time, tool spans, and post-call
  state decide verdicts; no output claims emotion or meaning.
- Two channels or a timestamped transcript in; a mono or mixed export is
  refused as NOT SCORABLE, never guessed at.
- Five dimensions (outcome, policy, conversation, speech, reliability), each
  reported on its own evidence; there is no blended score anywhere.
- Deterministic checks stay separate from the model-judged rubric lane, which
  is advisory and local.
- Determinism is verified in CI: byte-identical re-runs on Linux x86_64,
  Python 3.10, 3.11, and 3.12 ([VALIDATION.md](VALIDATION.md) Job 1).

## Every example is scoped to one run

A hotato example that shows a call failing on a named stack is labelled a
**provider-default** run: one assistant, one configuration, one date, one
scripted caller, on that provider's out-of-the-box interruption settings. It
demonstrates the threshold funnel scoped to that one run. Any stack, tuned, can
pass the same fixtures. hotato publishes fixtures and reproduction steps: a
record you can run yourself, not a scoreboard.

The full loop, command by command: [`docs/LIFECYCLE.md`](LIFECYCLE.md).
