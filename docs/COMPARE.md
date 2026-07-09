# Compare: Hotato vs broad QA platforms

Hotato is not your full QA platform.

**Use Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell for:** broad
session QA, synthetic simulation, task success, transcript rubrics, compliance
workflows, production dashboards, load testing.

**Use Hotato for:** local/private timing evidence from real calls,
CI-enforced regression tests via labelled fixtures, turn-taking proof,
refusing unsafe threshold bandaids. (A portable, trace-backed failure
contract ships in the release that adds the contract layer; today the same
job runs on a fixture and `hotato verify`, see [VALIDATION.md](VALIDATION.md).)

**Hotato answers:** "Did this exact production timing failure come back?"
**Hotato does not answer:** "Was the whole call successful?"

These are complementary layers, not competing ones. A team running one of the
platforms above for broad conversation QA or as the agent runtime itself still
needs an answer to a narrower question: the specific talk-over or false-stop
moment a real caller hit last week, is it still fixed after today's prompt
change? That is the layer Hotato owns, and it is deliberately the only layer
Hotato owns.

## What each named platform is built for

Capability descriptions only, drawn from each platform's own public launch and
product material, not from running Hotato against them. No performance claims,
no ranking.

| Platform | What it is built for |
|---|---|
| **Hamming** | Automated voice-agent testing: prompt-to-test generation, simulated call batteries, a multi-layer QA framework, and production-replay CI/CD regression across a broad test suite. |
| **Cekura** | AI voice/chat agent QA: production-conversation ingestion, test-case extraction from real calls, simulated scenario testing, and monitoring across fleets of agents, including regulated verticals. |
| **Coval** | Voice and chat agent simulation and evaluation, with published comparisons of testing approaches across the category. |
| **Bluejay** | Synthetic stress-testing for voice agents at scale. |
| **Roark** | Production-call replay for QA that preserves what the caller said, how, and when, for building regression scenarios. |
| **Vapi** | A voice agent orchestration platform: the runtime stack a team builds and deploys its agent on. |
| **Retell** | A voice agent orchestration platform for building and deploying voice agents. |

## The honest job, and the better fit

| What you need | Best fit | Why |
|---|---|---|
| Broad conversation QA: did the call succeed, was the transcript right, did it follow the rubric, simulate hundreds of scenarios, dashboards for the team | **Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell** | These grade the whole conversation, task success, and content, or they are the agent platform itself. Hotato does not do content, task success, or simulation. |
| Prevent an interruption problem **in the moment**, at runtime: predict endpointing, suppress barge-in on noise, tune the live turn detector | **Krisp, Pipecat, LiveKit** (and similar runtime layers) | These act during the live call. Hotato never runs at runtime and never touches a live call. |
| Prove a specific timing bug is fixed and **stays** fixed, from a real recorded call, portably, without sending audio anywhere | **Hotato** | A private, deterministic fixture: audio, a human label, and an explicit policy, scored the same way everywhere with `hotato verify`. Ships as a single portable contract bundle -- audio, timing evidence, trace evidence, label, policy, CI command -- in the release that adds the contract layer. |

If your open question is "is my agent good?", start with a QA platform. If it
is "is my agent interrupting well right now?", start with a runtime layer. If
it is "did the talk-over bug we fixed last month come back in this release?",
that is Hotato.

## Runtime layer vs regression layer

These two are often confused, so it is worth being exact.

- A **runtime layer** (Krisp, Pipecat, LiveKit turn detection) makes a decision
  *during* the call: should the agent stop talking now? It optimizes the live
  experience. It does not keep a durable, re-runnable record of whether a
  specific past moment is handled correctly.
- A **regression layer** (Hotato) makes a decision *after* the call, from the
  recording: given this exact audio and this label, did the timing match, yes
  or no, byte-stable on every run? It exists to catch the day a fixed bug
  silently returns, and to hand that proof to CI as a portable artifact.

You want both. A runtime layer improves the median call. A regression layer
stops a good fix from quietly rotting three releases later. Hotato takes the
moment a runtime layer got wrong, freezes it as a fixture with `hotato
fixture promote`, and fails CI if it regresses.

## What Hotato is for, precisely

- **Private.** Scoring, scanning, reports, fixtures, and verification run
  offline. Audio stays on your machine unless you explicitly pull it from your
  own stack. Nothing is uploaded to Attention Labs. See
  [THREAT-MODEL.md](THREAT-MODEL.md).
- **Deterministic.** No learned score, no sampling. The same recording produces
  the same timing numbers every run, so a red build means the audio changed.
- **Portable.** A confirmed failure becomes a labelled fixture (audio, human
  label, explicit policy) that travels with the repository and verifies the
  same way on any machine with `hotato verify`. It ships as a self-contained
  contract bundle -- adding timing evidence, trace evidence, and a CI command
  in one artifact -- in the release that adds the contract layer.
- **Narrow on purpose.** Three timing signals: talking over the caller,
  false-stopping on a backchannel, yielding too slowly. It does not grade
  content, intent, or outcomes.

## On provider-default examples

When a Hotato example shows a call failing on a named stack, it is labelled a
**provider-default** run: one assistant, one configuration, one date, one
scripted caller, on that provider's out-of-the-box interruption settings. It
demonstrates the threshold funnel (how a default config can miss an
interruption and false-stop on a backchannel in the same battery). It is
**not** a vendor benchmark and **not** a ranking of one platform against
another. Any stack, tuned, can pass the same fixtures. Hotato never publishes a
scoreboard, of its own results or anyone else's.

## The short version

Use a QA platform for broad quality, or one of the named orchestration
platforms as your agent's runtime. Use a runtime layer for live interruption
handling. Use Hotato when you need to prove, privately and portably, that a
specific timing bug from a real call is fixed and does not come back. They
compose; none of them replaces the others.
