# Compare: Hotato vs broad QA platforms

Hotato is an open-source, self-hosted conversation-QA system; this page maps where it sits next to runtime layers and hosted platforms.

**Use Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell for:** broad
session QA, synthetic simulation, task success, transcript rubrics, compliance
workflows, production dashboards, load testing.

**Use Hotato for:** portable failure contracts from real calls, local/private
timing evidence, CI-enforced regression tests, trace-backed turn-taking proof,
refusing unsafe threshold bandaids.

**Hotato answers:** "Is the recorded evidence for this exact timing failure still intact (every push), and does the CURRENT agent still avoid it (on a fresh recapture, [`docs/RECAPTURE.md`](RECAPTURE.md))?"
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

## What it measures, and the better fit

| What you need | Best fit | Why |
|---|---|---|
| Broad conversation QA: did the call succeed, was the transcript right, did it follow the rubric, simulate hundreds of scenarios, dashboards for the team | **Hamming, Cekura, Coval, Bluejay, Roark, Vapi, or Retell** | These grade the whole conversation, task success, and content, or they are the agent platform itself. Hotato does not do content, task success, or simulation. |
| Prevent an interruption problem **in the moment**, at runtime: predict endpointing, suppress barge-in on noise, tune the live turn detector | **Pipecat, LiveKit** (and similar runtime layers) | These act during the live call. Hotato never runs at runtime and never touches a live call. |
| Prove a specific timing bug is fixed, from a real recorded call, portably, without sending audio anywhere; a fresh recapture pins whether it **stays** fixed | **Hotato** | A private, deterministic fixture: audio, a human label, and an explicit policy, scored the same way everywhere with `hotato verify`. Ships as a single portable contract bundle -- audio, timing evidence, trace evidence, label, policy, CI command -- in the release that adds the contract layer. |

If your open question is "is my agent good?", start with a QA platform. If it
is "is my agent interrupting well right now?", start with a runtime layer. If
it is "is the evidence for the talk-over bug we fixed last month still
intact, and does this release's agent still avoid it?", that is Hotato: the
frozen check runs on every push, and the agent check needs a fresh recapture
([`docs/RECAPTURE.md`](RECAPTURE.md)).

## Runtime layer vs regression layer

These two are often confused, so it is worth being exact.

- A **runtime layer** (Pipecat, LiveKit turn detection) makes a decision
  *during* the call: should the agent stop talking now? It optimizes the live
  experience. It does not keep a durable, re-runnable record of whether a
  specific past moment is handled correctly.
- A **regression layer** (Hotato) makes a decision *after* the call, from the
  recording: given this exact audio and this label, did the timing match, yes
  or no, byte-stable on every run? On the frozen recording it catches a
  change to that recorded evidence or policy on every push, and hands that
  proof to CI as a portable artifact; catching the day the AGENT itself
  regresses needs a fresh recapture through the same check
  ([`docs/RECAPTURE.md`](RECAPTURE.md)).

You want both. A runtime layer improves the median call. A regression layer
catches evidence and policy drift on every push, and, recaptured, proves
whether a good fix is still holding three releases later. Hotato takes the
moment a runtime layer got wrong, freezes it as a fixture with `hotato
fixture promote`, fails CI if the frozen evidence regresses, and recaptures
to prove the live agent still holds.

## What Hotato is for, precisely

- **Private.** Scoring, scanning, reports, fixtures, and verification run
  offline. Audio stays on your machine unless you explicitly pull it from your
  own stack. Nothing is uploaded to Attention Labs. See
  [THREAT-MODEL.md](THREAT-MODEL.md).
- **Deterministic under pinned inputs.** No learned score, no sampling. With
  the same supported hotato version, the same audio, channel map, event
  onset, label, and scoring config, the reference backend produces the same
  timing numbers every run, so a changed result means at least one pinned
  input, policy, or scorer component changed -- most commonly the audio
  itself. Byte-identical re-runs are verified in CI on Linux x86_64, Python
  3.10, 3.11, and 3.12. The same check now also runs in CI on macOS and
  Windows, not yet green -- see [VALIDATION.md](VALIDATION.md) Job 1.
- **Portable.** A confirmed failure becomes a labelled fixture (audio, human
  label, explicit policy) that travels with the repository and verifies the
  same way with `hotato verify`, deterministic for a fixed hotato version
  (see above). It ships as a self-contained
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
specific timing bug from a real call is fixed, gate every push on the frozen
evidence for it, and recapture to confirm it stays that way on your current
agent. They compose; none of them replaces the others.
