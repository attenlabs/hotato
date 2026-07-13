# The starter kit: `hotato init starter`

The fastest way to add hotato to an existing voice-agent repository. It
scaffolds the CI gate, a stack-tuned config file, and the three directories
the rest of the docs assume already exist, so you can go straight to turning
your first bad call into a contract instead of wiring plumbing by hand.

```bash
hotato init starter --stack vapi --out .
```

`--stack` is one of `vapi`, `retell`, `twilio`, `livekit`, `pipecat` -- every
stack hotato has a shipped connector for today (see
[`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)). `--out` is usually `.`, the root of
the repo you are adding hotato to. Generation runs offline, in one command,
nothing to connect.

## What it writes

```
HOTATO.md                                # what was added, next steps (read this first)
hotato.yaml                              # config skeleton, tuned for --stack
.gitignore                               # excludes local/pulled recordings;
                                          #   keeps pinned fixture/contract clips committed
.github/workflows/hotato-contracts.yml   # the CI gate
fixtures/
  README.md
  scenarios/.gitkeep                     # -> hotato fixture create --out fixtures
  audio/.gitkeep
contracts/
  README.md
  .gitkeep                               # -> hotato contract create --out contracts
reports/
  README.md
  .gitkeep                               # local/CI scratch: doctor/report/sweep output
```

Every file writes cleanly only when it doesn't already exist (pass `--force`
to overwrite); each write lands whole or not at all. The generated file names
are deliberately namespaced away from an existing repo's own files (`HOTATO.md`,
distinct from `README.md`; `hotato-contracts.yml`, distinct from
`hotato.yml`) so a first run drops in cleanly alongside files a voice-agent
repo almost always already has.

## Two input paths, chosen for you by `--stack`

**Auto-pull** (`vapi`, `retell`, `twilio`): hotato fetches the recording
itself once you connect a key. `hotato.yaml`'s `credentials.env` names the
exact environment variable(s) (`VAPI_API_KEY`; `RETELL_API_KEY`;
`TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`) `hotato connect <stack>` also
reads. `recording.access` is `auto-pull`.

**Capture-in-your-infra** (`livekit`, `pipecat`): capture happens inside your
own deployment, so no credentials are needed.
`hotato.yaml`'s `credentials.env` is `[]` and `recording.access` is
`capture-in-your-infra`; `hotato setup --stack <stack>` prints the exact
two-track capture scaffold, and you point `hotato contract create --stereo`
at the WAV your own deployment writes.

## LiveKit and Pipecat runbook

LiveKit and Pipecat are the two stacks where capture and the turn-taking
config both live in your own code, ahead of any vendor API. This is the
operator runbook for both, capture through CI.

### LiveKit

1. **Capture.** Two audio-only Track egresses, one per participant --
   RoomComposite mixes both parties into one channel and cannot attribute
   overlap. `hotato setup --stack livekit` prints the copy-paste scaffold
   (Python `livekit-api`, `TrackEgressRequest` + `DirectFileOutput`); a
   ready-to-copy version also lives at `adapters/livekit_capture.py`.
2. **Find the turn-taking config.** It lives on
   `AgentSession(turn_handling=TurnHandlingOptions(...))`: `turn_detection`
   (`inference.TurnDetector()` / `"realtime_llm"` / `"vad"` / `"stt"` /
   `"manual"`), `endpointing` (`min_delay`, `max_delay`), and `interruption`
   (`enabled`, `mode`, `min_duration`, `min_words`,
   `false_interruption_timeout`, `resume_false_interruption`). Read what a
   given agent file is running, statically, before you propose
   changing anything: `hotato inspect --stack livekit --config agent.py`.
3. **Score it.**
   `hotato capture --stack livekit --caller caller.wav --agent agent.wav --onset <sec> --expect yield`
   (convert the egress output to WAV first, e.g. `ffmpeg -i caller.ogg caller.wav`).
4. **Fixture, contract, CI.** Same as every stack from here -- see "Turn
   your first bad call into a contract" below.

### Pipecat

1. **Capture.** A 2-channel `AudioBufferProcessor` in-pipeline (channel 0 =
   user/caller, channel 1 = bot/agent) -- do not mix down to one channel.
   `hotato setup --stack pipecat` prints the copy-paste scaffold; a
   ready-to-copy version also lives at `adapters/pipecat_capture.py`.
2. **Find the turn-taking config.** It lives on `PipelineTask`'s user-turn
   strategies: start strategies (`VADUserTurnStartStrategy`,
   `TranscriptionUserTurnStartStrategy`,
   `MinWordsUserTurnStartStrategy(min_words=...)`,
   `KrispVivaIPUserTurnStartStrategy(...)`) and stop strategies
   (`SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)`,
   `TurnAnalyzerUserTurnStopStrategy(turn_analyzer=...)`); note
   `MinWordsInterruptionStrategy` is deprecated since pipecat 0.0.99 in
   favor of `MinWordsUserTurnStartStrategy`. Read what a given bot file is
   running, statically: `hotato inspect --stack pipecat --config bot.py`.
3. **Score it.**
   `hotato capture --stack pipecat --stereo captured.wav --expect yield`
   (write the WAV from the `AudioBufferProcessor`'s `on_audio_data` handler
   first).
4. **Fixture, contract, CI.** Same as every stack from here -- see "Turn
   your first bad call into a contract" below.

Both APIs move; `hotato setup` and `hotato inspect` state the verified-against
date. Full field-level detail and provenance: [`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)
(capture) and [`FIX-PLANS.md`](FIX-PLANS.md) (inspect, Level 1 of the fix ladder).

## The CI gate

`.github/workflows/hotato-contracts.yml` runs on push, on pull request, and
weekly. It is two guarded steps that pass clean, as a **no-op**, until you
have added a first contract or fixture (a fresh scaffold's normal starting
state):

```bash
hotato contract verify contracts --junit hotato.xml --format json > contracts-verify.json
hotato run --scenarios fixtures/scenarios --audio fixtures/audio --format json > fixtures-run.json
```

The JUnit file is published as a build artifact on every run (`always()`),
whether the gate passed, failed, or had nothing to check yet.

For the three auto-pull stacks, the workflow also carries a `weekly-sweep`
job: a passive, candidate-only sweep of recent calls
(`hotato sweep --stack <stack>`), ranked by hotato's own salience -- a
candidate list for you to review and label. It ships **disabled** (`if:
false`): flip it to `true` once the stack's credential env var(s) are set as
repo secrets (Settings -> Secrets and variables -> Actions). A live pull
against your account only runs on your explicit say-so: enabling this job is
a human decision, made once, in your own CI config. `livekit`/`pipecat` skip
this job entirely, since capture for those stacks already happens inside
your own deployment.

## Turn your first bad call into a contract

```bash
# auto-pull stacks
hotato connect vapi --api-key <key>
hotato sweep --stack vapi --out hotato-sweep.html
# open hotato-sweep.html, pick a candidate moment, then:
hotato contract create --from-candidate hotato-sweep.json#1 \
    --expect yield --id refund-cutoff-001 --out contracts

# capture-in-your-infra stacks
hotato setup --stack livekit
# once your deployment writes a two-channel WAV:
hotato contract create --stereo call.wav --onset 42.18 \
    --expect yield --id refund-cutoff-001 --out contracts
```

Commit the resulting `contracts/refund-cutoff-001.hotato/` directory. The
next push runs it through the CI gate above.

**A contract bundle contains call audio** (`audio/event.wav`). If this repo
is or could become public, commit a sanitized fixture (synthetic or
consent-cleared), and keep customer contracts in a private repository
or controlled artifact storage. See [`CONTRACTS.md`](CONTRACTS.md).

## Read more

- The bundle layout and the create/verify/inspect/pack/unpack commands:
  [`CONTRACTS.md`](CONTRACTS.md)
- The underlying fixture primitive, one bad call to a CI gate in five steps:
  [`BAD-CALL-TO-CI.md`](BAD-CALL-TO-CI.md)
- Per-stack connector support, verified against the vendor's live docs:
  [`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)
- The connect-once bulk pull-and-analyze recipe: [`CONNECT.md`](CONNECT.md)
- An agent adding hotato to a repo end to end: [`../AGENTS.md`](../AGENTS.md)
