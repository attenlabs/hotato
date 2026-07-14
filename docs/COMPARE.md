# Compare: Hotato vs broad QA platforms

Hotato is an open-source, self-hosted conversation-QA system. This page maps where it sits next to runtime layers and hosted platforms.

**Use Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell for:** broad
session QA, synthetic simulation, task success, transcript rubrics, compliance
workflows, production dashboards, load testing.

**Use Hotato for:** portable failure contracts from live calls, local/private
timing evidence, CI-enforced regression tests, trace-backed turn-taking proof,
refusing unsafe threshold bandaids.

**Hotato answers:** "Is the recorded evidence for this exact timing failure still intact (every push), and does the CURRENT agent still avoid it (on a fresh recapture, [`docs/RECAPTURE.md`](RECAPTURE.md))?"
**A broad QA platform answers:** "Was the whole call successful?"

These are complementary layers. A team running one of those platforms for broad
QA, or as the agent runtime itself, still needs the narrower answer: the
specific talk-over or false-stop moment a caller hit last week, is it still
fixed after today's prompt change? That is the layer Hotato owns.

## What each named platform is built for

Capability descriptions only, sourced entirely from each platform's own public
launch and product material.

| Platform | What it is built for |
|---|---|
| **Hamming** | Automated voice-agent testing: prompt-to-test generation, simulated call batteries, a multi-layer QA framework, and production-replay CI/CD regression across a broad test suite. |
| **Cekura** | AI voice/chat agent QA: production-conversation ingestion, test-case extraction from production calls, simulated scenario testing, and monitoring across fleets of agents, including regulated verticals. |
| **Coval** | Voice and chat agent simulation and evaluation, with published comparisons of testing approaches across the category. |
| **Bluejay** | Synthetic stress-testing for voice agents at scale. |
| **Roark** | Production-call replay for QA that preserves what the caller said, how, and when, for building regression scenarios. |
| **Vapi** | A voice agent orchestration platform: the runtime stack a team builds and deploys its agent on. |
| **Retell** | A voice agent orchestration platform for building and deploying voice agents. |

## What it measures, and the better fit

| What you need | Best fit | Why |
|---|---|---|
| Broad conversation QA: did the call succeed, was the transcript right, did it follow the rubric, simulate hundreds of scenarios, dashboards for the team | **Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell** | These grade the whole conversation, task success, and content, or they are the agent platform itself. Hotato's lane is the timing evidence underneath. |
| Prevent an interruption problem **in the moment**, at runtime: predict endpointing, suppress barge-in on noise, tune the live turn detector | **Pipecat, LiveKit** (and similar runtime layers) | These act during the live call. Hotato runs after the fact, from the recording. |
| Prove a specific timing bug is fixed, from a recorded call, portably, with every byte staying on your machine; a fresh recapture pins whether it **stays** fixed | **Hotato** | A private, deterministic fixture: audio, a human label, and an explicit policy, scored the same way everywhere with `hotato verify`. Ships as a single portable contract bundle -- audio, timing evidence, trace evidence, label, policy, CI command. |

Rule of thumb: "is my agent good?" -> a QA platform. "is my agent
interrupting well right now?" -> a runtime layer. "is the evidence for the
talk-over bug we fixed last month still intact, and does this release's agent
still avoid it?" -> Hotato (the frozen check runs on every push; the agent
check needs a fresh recapture, [`docs/RECAPTURE.md`](RECAPTURE.md)).

## Runtime layer vs regression layer

These two are often confused, so it is worth being exact.

- A **runtime layer** (Pipecat, LiveKit turn detection) decides *during* the
  call: should the agent stop talking now? It optimizes the live experience,
  moment to moment.
- A **regression layer** (Hotato) decides *after* the call, from the recording:
  given this exact audio and this label, did the timing match, byte-stable on
  every run? On the frozen recording it catches a change to that recorded
  evidence or policy on every push, and hands that proof to CI; catching the day
  the AGENT itself regresses needs a fresh recapture through the same check
  ([`docs/RECAPTURE.md`](RECAPTURE.md)).

You want both. A runtime layer improves the median call. A regression layer
catches evidence and policy drift on every push and, recaptured, shows whether a
good fix still holds three releases later. Hotato takes the moment a runtime
layer got wrong, freezes it as a fixture with `hotato fixture promote`, fails CI
if the frozen evidence regresses, and recaptures to check the live agent.

## What Hotato is for, precisely

- **Private.** Scoring, scanning, reports, fixtures, and verification run
  offline, and audio stays on your machine unless you explicitly pull it from
  your own stack. See [THREAT-MODEL.md](THREAT-MODEL.md).
- **Deterministic under pinned inputs.** The reference backend is rule-based end
  to end: with the same supported hotato version, audio, channel map, event
  onset, label, and scoring config, it produces the same timing numbers every
  run, so a changed result means at least one pinned input, policy, or scorer
  component changed -- most commonly the audio itself. Byte-identical re-runs
  are verified in CI on Linux x86_64, Python 3.10, 3.11, and 3.12 -- see
  [VALIDATION.md](VALIDATION.md) Job 1.
- **Portable.** A confirmed failure becomes a labelled fixture (audio, human
  label, explicit policy) that travels with the repository and verifies the same
  way with `hotato verify`, deterministic for a fixed hotato version.
- **Narrow on purpose.** Three timing signals, and only those: talking over the
  caller, false-stopping on a backchannel, yielding too slowly.

## On provider-default examples

When a Hotato example shows a call failing on a named stack, it is labelled a
**provider-default** run: one assistant, one configuration, one date, one
scripted caller, on that provider's out-of-the-box interruption settings. It
demonstrates the threshold funnel (how a default config can miss an
interruption and false-stop on a backchannel in the same battery), scoped to
that one run. Any stack, tuned, can pass the same fixtures. Hotato publishes
fixtures and reproduction steps, never a scoreboard.

## The short version

Use a QA platform for broad quality, a runtime layer for live interruption
handling, and Hotato to prove, privately and portably, that a specific timing
bug from a call is fixed, gate every push on the frozen evidence, and recapture
to confirm it stays that way on your current agent.
