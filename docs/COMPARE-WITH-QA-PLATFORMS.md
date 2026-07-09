# Where Hotato fits alongside your QA platform

Hotato is one layer of voice-agent quality, not a replacement for your eval
platform. It does a single job: **private, deterministic, post-call timing
regression from your own recordings.** For most teams it sits next to a broader
QA tool and a runtime interruption layer, and each does what it is best at.

This page is a routing guide. If another tool is a better fit for what you need,
that is what you should use, and this page says so plainly.

## The honest job, and the better fit

| What you need | Best fit | Why |
|---|---|---|
| Broad conversation QA: did the call succeed, was the transcript right, did it follow the rubric, simulate hundreds of scenarios, dashboards for the team | **Vapi, Retell, Hamming** (and similar QA / simulation platforms) | These grade the whole conversation, task success, and content. They run large simulated batteries and give you a hosted dashboard. Hotato does not do content, task success, or simulation. |
| Prevent an interruption problem **in the moment**, at runtime: predict endpointing, suppress barge-in on noise, tune the live turn detector | **Krisp, Pipecat, LiveKit** (and similar runtime layers) | These act during the live call. Hotato never runs at runtime and never touches a live call. |
| Prove a specific timing bug is fixed and **stays** fixed, from real recorded calls, without sending audio anywhere | **Hotato** | Deterministic post-call scoring, offline, that turns a confirmed bug into a permanent CI fixture. |

If your open question is "is my agent good?", start with a QA platform. If it is
"is my agent interrupting well right now?", start with a runtime layer. If it is
"did the talk-over bug we fixed last month come back in this release?", that is
Hotato.

## Runtime layer vs regression layer

These two are often confused, so it is worth being exact.

- A **runtime layer** (Krisp, Pipecat, LiveKit turn detection) makes a decision
  *during* the call: should the agent stop talking now? It optimizes the live
  experience. It does not keep a durable, re-runnable record of whether a
  specific past moment is handled correctly.
- A **regression layer** (Hotato) makes a decision *after* the call, from the
  recording: given this exact audio and this label, did the timing match, yes or
  no, byte-stable on every run? It exists to catch the day a fixed bug silently
  returns.

You want both. A runtime layer improves the median call. A regression layer
stops a good fix from quietly rotting three releases later. Hotato takes the
moment a runtime layer got wrong, freezes it as a fixture, and fails CI if it
regresses.

## What Hotato is for, precisely

- **Private.** Scoring, scanning, reports, fixtures, and verification run
  offline. Audio stays on your machine unless you explicitly pull it from your
  own stack. Nothing is uploaded to Attention Labs. See
  [THREAT-MODEL.md](THREAT-MODEL.md).
- **Deterministic.** No learned score, no sampling. The same recording produces
  the same timing numbers every run, so a red build means the audio changed.
- **Regression-shaped.** A confirmed bug becomes a fixture
  (`hotato fixture promote`) that gates CI. `verify` proves a fix across the
  whole battery and reports coincidence, not causation.
- **Narrow on purpose.** Three timing signals: talking over the caller,
  false-stopping on a backchannel, yielding too slowly. It does not grade
  content, intent, or outcomes.

## On provider-default examples

When a Hotato example shows a call failing on a named stack, it is labelled a
**provider-default** run: one assistant, one configuration, one date, one
scripted caller, on that provider's out-of-the-box interruption settings. It
demonstrates the threshold funnel (how a default config can miss an interruption
and false-stop on a backchannel in the same battery). It is **not** a vendor
benchmark and **not** a ranking of one platform against another. Any stack,
tuned, can pass the same fixtures. Hotato never publishes a scoreboard.

## The short version

Use a QA platform for broad quality. Use a runtime layer for live interruption
handling. Use Hotato when you need to prove, privately and deterministically,
that a specific timing bug from a real call is fixed and does not come back.
They compose; none of them replaces the others.
