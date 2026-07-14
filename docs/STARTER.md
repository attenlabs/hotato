# The starter kit: `hotato init starter`

The fastest way to add hotato to an existing voice-agent repo: one command
scaffolds the CI gate, a stack-tuned config file, and the three directories
the rest of the docs assume already exist.

```bash
hotato init starter --stack vapi --out .
```

`--stack` is one of `vapi`, `retell`, `twilio`, `livekit`, `pipecat` --
every stack hotato has a shipped connector for
([`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)). `--out` is usually `.`, the
repo root. Generation runs offline -- nothing to connect.

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

Each file writes only when it doesn't already exist (`--force` to
overwrite), whole or not at all. Names are namespaced away from your own
files (`HOTATO.md`, not `README.md`; `hotato-contracts.yml`, not
`hotato.yml`), so a first run drops in cleanly next to files you already
have.

## Two input paths, chosen by `--stack`

**Auto-pull** (`vapi`, `retell`, `twilio`): hotato fetches the recording
itself once you connect a key. `hotato.yaml`'s `credentials.env` names the
exact variable(s) (`VAPI_API_KEY`; `RETELL_API_KEY`; `TWILIO_ACCOUNT_SID`
+ `TWILIO_AUTH_TOKEN`), the same ones `hotato connect <stack>` reads.
`recording.access` is `auto-pull`.

**Capture-in-your-infra** (`livekit`, `pipecat`): capture happens inside
your own deployment, so no credentials are needed. `credentials.env` is
`[]`, `recording.access` is `capture-in-your-infra`; `hotato setup --stack
<stack>` prints the two-track capture scaffold, and you point `hotato
contract create --stereo` at the WAV your deployment writes.

## LiveKit and Pipecat runbook

LiveKit and Pipecat are the two stacks where capture and turn-taking
config live in your own code, ahead of any vendor API. Runbook for both,
capture through CI.

### LiveKit

1. **Capture.** Two audio-only Track egresses, one per participant --
   RoomComposite mixes both parties into one channel and can't attribute
   overlap. `hotato setup --stack livekit` prints the scaffold (Python
   `livekit-api`, `TrackEgressRequest` + `DirectFileOutput`); a
   ready-to-copy version lives at `adapters/livekit_capture.py`.
2. **Find the turn-taking config.** On `AgentSession(turn_handling=
   TurnHandlingOptions(...))`: `turn_detection`
   (`inference.TurnDetector()` / `"realtime_llm"` / `"vad"` / `"stt"` /
   `"manual"`), `endpointing` (`min_delay`, `max_delay`), and
   `interruption` (`enabled`, `mode`, `min_duration`, `min_words`,
   `false_interruption_timeout`, `resume_false_interruption`). Check what
   an agent file runs, statically:
   `hotato inspect --stack livekit --config agent.py`.
3. **Score it.**
   `hotato capture --stack livekit --caller caller.wav --agent agent.wav --onset <sec> --expect yield`
   (convert the egress output to WAV first: `ffmpeg -i caller.ogg caller.wav`).
4. **Fixture, contract, CI.** Same as every stack -- see "Turn your first
   bad call into a contract" below.

### Pipecat

1. **Capture.** A 2-channel `AudioBufferProcessor` in-pipeline (channel 0
   = user/caller, channel 1 = bot/agent) -- don't mix down to one channel.
   `hotato setup --stack pipecat` prints the scaffold; a ready-to-copy
   version lives at `adapters/pipecat_capture.py`.
2. **Find the turn-taking config.** On `PipelineTask`'s user-turn
   strategies: start (`VADUserTurnStartStrategy`,
   `TranscriptionUserTurnStartStrategy`,
   `MinWordsUserTurnStartStrategy(min_words=...)`,
   `KrispVivaIPUserTurnStartStrategy(...)`) and stop
   (`SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)`,
   `TurnAnalyzerUserTurnStopStrategy(turn_analyzer=...)`) --
   `MinWordsInterruptionStrategy` is deprecated since pipecat 0.0.99 in
   favor of `MinWordsUserTurnStartStrategy`. Check what a bot file runs,
   statically: `hotato inspect --stack pipecat --config bot.py`.
3. **Score it.**
   `hotato capture --stack pipecat --stereo captured.wav --expect yield`
   (write the WAV from `AudioBufferProcessor`'s `on_audio_data` handler
   first).
4. **Fixture, contract, CI.** Same as every stack -- see "Turn your first
   bad call into a contract" below.

Both APIs move; `hotato setup` and `hotato inspect` print the
verified-against date. Full field-level detail:
[`ADAPTER-STATUS.md`](ADAPTER-STATUS.md) (capture) and
[`FIX-PLANS.md`](FIX-PLANS.md) (inspect, Level 1 of the fix ladder).

## The CI gate

`.github/workflows/hotato-contracts.yml` runs on push, pull request, and
weekly. Two guarded steps that pass clean, as a no-op, until you add a
first contract or fixture (a fresh scaffold's normal starting state):

```bash
hotato contract verify contracts --junit hotato.xml --format json > contracts-verify.json
hotato run --scenarios fixtures/scenarios --audio fixtures/audio --format json > fixtures-run.json
```

The JUnit file publishes as a build artifact on every run (`always()`),
whether the gate passed, failed, or had nothing to check yet.

The three auto-pull stacks also get a `weekly-sweep` job: a passive,
candidate-only sweep of recent calls (`hotato sweep --stack <stack>`),
ranked by salience for you to review and label. It ships disabled (`if:
false`) -- flip it to `true` once the stack's credential env var(s) are
repo secrets (Settings -> Secrets and variables -> Actions). A live pull
runs only on your say-so, made once, in your own CI config.
`livekit`/`pipecat` skip this job; capture already happens in your own
deployment.

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

**A contract bundle contains call audio** (`audio/event.wav`). If this
repo is or could become public, commit a sanitized fixture (synthetic or
consent-cleared) and keep customer contracts in a private repository or
controlled artifact storage. See [`CONTRACTS.md`](CONTRACTS.md).

## Read more

- The bundle layout and the create/verify/inspect/pack/unpack commands:
  [`CONTRACTS.md`](CONTRACTS.md)
- The underlying fixture primitive, one bad call to a CI gate in five steps:
  [`BAD-CALL-TO-CI.md`](BAD-CALL-TO-CI.md)
- Per-stack connector support, verified against the vendor's live docs:
  [`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)
- The connect-once bulk pull-and-analyze recipe: [`CONNECT.md`](CONNECT.md)
- An agent adding hotato to a repo end to end: [`../AGENTS.md`](../AGENTS.md)
