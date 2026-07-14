# Compare: Hotato vs broad QA platforms

Hotato is an open-source, self-hosted conversation-QA system. This page
maps where it sits next to runtime layers and hosted platforms.

**Use Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell for:** broad
session QA, synthetic simulation, task success, transcript rubrics,
compliance workflows, production dashboards, load testing.

**Use Hotato for:** portable failure contracts from live calls,
local/private timing evidence, CI-enforced regression tests, trace-backed
turn-taking proof, refusing unsafe threshold bandaids.

**Hotato answers:** "Is the recorded evidence for this exact timing
failure still intact (every push), and does the CURRENT agent still avoid
it (on a fresh recapture, [`docs/RECAPTURE.md`](RECAPTURE.md))?"
**A broad QA platform answers:** "Was the whole call successful?"

The two are complementary. A team running one of those platforms for
broad QA, or as the agent runtime itself, still needs the narrower answer:
is the specific talk-over or false-stop moment a caller hit last week
still fixed after today's prompt change? That is the layer Hotato owns.

## What each platform is built for

Capability descriptions only, sourced from each platform's own public
launch and product material.

| Platform | Built for |
|---|---|
| **Hamming** | Prompt-to-test generation, simulated call batteries, production-replay CI/CD regression |
| **Cekura** | Production-call ingestion, test-case extraction, simulated scenarios, fleet monitoring, regulated verticals |
| **Coval** | Voice/chat agent simulation and evaluation, with published testing-approach comparisons |
| **Bluejay** | Synthetic stress-testing for voice agents at scale |
| **Roark** | Production-call replay QA, preserving what the caller said, how, and when |
| **Vapi** | Voice agent orchestration -- the runtime stack you build and deploy your agent on |
| **Retell** | Voice agent orchestration for building and deploying voice agents |

## Three layers, three jobs

Easy to confuse, so it's worth being exact about which one answers which
question:

| Job | Best fit | Why |
|---|---|---|
| Grade the whole call -- QA, transcript rubrics, task success, dashboards | Hamming, Cekura, Coval, Bluejay, Roark, Vapi, Retell | Grades the whole conversation and content, or is the agent platform itself |
| Catch an interruption live -- predict endpointing, suppress barge-in on noise, tune the turn detector | Pipecat, LiveKit (and similar runtime layers) | Acts *during* the call; Hotato runs *after*, from the recording |
| Prove one specific timing bug stays fixed, portably, byte-stable, on your machine | Hotato | A private, deterministic fixture -- audio, a human label, an explicit policy -- scored the same way everywhere with `hotato verify`; a fresh recapture ([`docs/RECAPTURE.md`](RECAPTURE.md)) checks whether the live agent still avoids it |

Run every layer you need. A runtime layer improves the median call. Hotato
catches evidence and policy drift on every push and, recaptured, shows
whether a fix holds releases later: freeze the moment a runtime layer got
wrong with `hotato fixture promote`, fail CI when the frozen evidence
regresses, and recapture ([`docs/RECAPTURE.md`](RECAPTURE.md)) to check
the live agent.

## What Hotato is for, precisely

- **Private.** Scoring, scanning, reports, fixtures, and verification run
  offline -- audio stays on your machine unless you explicitly pull it
  from your own stack. See [THREAT-MODEL.md](THREAT-MODEL.md).
- **Deterministic under pinned inputs.** The reference backend is
  rule-based end to end: the same hotato version, audio, channel map,
  event onset, label, and scoring config always produce the same numbers
  -- a changed result means a pinned input, policy, or scorer component
  changed, most commonly the audio itself. Byte-identical re-runs are
  verified in CI on Linux x86_64, Python 3.10, 3.11, and 3.12:
  [VALIDATION.md](VALIDATION.md) Job 1.
- **Portable.** A confirmed failure becomes a labelled fixture (audio,
  human label, explicit policy) that travels with the repository and
  verifies the same way anywhere with `hotato verify`, deterministic for
  a fixed hotato version.
- **Narrow on purpose.** Three timing signals, and only those: talking
  over the caller, false-stopping on a backchannel, yielding too slowly.

## Every example is scoped to one run

A Hotato example that shows a call failing on a named stack is labelled a
**provider-default** run: one assistant, one configuration, one date, one
scripted caller, on that provider's out-of-the-box interruption settings.
It demonstrates the threshold funnel -- a default config missing an
interruption and false-stopping on a backchannel in the same battery --
scoped to that one run. Any stack, tuned, can pass the same fixtures.
Hotato publishes fixtures and reproduction steps: a record you can run
yourself, not a scoreboard.

## The short version

A QA platform grades the whole call. A runtime layer decides what the
agent does live. Hotato proves, privately and portably, that a specific
timing bug from a call is fixed, gates every push on the frozen evidence,
and recaptures to confirm it still holds on your current agent.
