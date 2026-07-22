# Compare: hotato vs hosted platforms

Local-first testing and observability for AI agents, next to the hosted
platforms that run the same jobs as a service. The short version: the same
lifecycle, three structural differences.

## Property by property

The hosted platforms (LangSmith, Langfuse, Phoenix, LangWatch, and the
voice-specific tools) are capable products: many support deterministic code
evaluators, datasets, experiments, human review, and self-hosting. This table
compares default properties, not a caricature, and it is honest about where a
managed platform is the stronger fit.

| Property | hotato default | Typical managed deployment |
|---|---|---|
| Raw evidence location | your local filesystem or VPC | the vendor's service |
| Price at scale | free and MIT, any volume | metered per seat, run, or event |
| Deterministic timing lane (turn-taking, say-do) | built in | varies by platform |
| Content-addressed contracts + release proofs | built in | platform-specific |
| Offline verification, no service dependency | built in | often service-dependent |
| Model-judge lane | separate and advisory by default | commonly integrated into the score |
| Hosted parallel execution at scale | you operate it | managed by the vendor |
| Persistent trace search, datasets, prompt management | limited | commonly built in |
| Team collaboration, roles, review queues | local workspace | mature hosted collaboration |

## Where hotato is the clear pick

- **Your data stays yours.** Recordings, traces, prompts, and backend state
  never leave your machine, and a contract or proof stays verifiable if you
  stop using hotato, if the vendor changes, or if the service is down.
- **Verdicts you can gate on.** The deterministic lanes score the same input
  the same way on every machine, byte for byte, so an exit code can block a
  merge. A model-judged score that varies run to run cannot.
- **Free at any volume.** No per-seat, per-run, or per-event meter anywhere.

## Where a managed platform may fit better

A hosted platform is often the faster path if you need mature multi-user
collaboration, a persistent searchable trace explorer with saved views,
built-in dataset and prompt management, or vendor-operated execution at high
concurrency. hotato is deliberately local and CLI-first; those surfaces are
limited today. The two compose: point a hosted platform's OTel export at
`hotato trace ingest` and keep the deterministic test and release-evidence
layer on your own machine.

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
