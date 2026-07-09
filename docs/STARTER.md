# The starter kit: `hotato init starter`

The fastest way to add hotato to an existing voice-agent repository. It
scaffolds the CI gate, a stack-tuned config file, and the three directories
the rest of the docs assume already exist, so you can go straight to turning
your first bad call into a contract instead of wiring plumbing by hand.

```bash
hotato init starter --stack vapi --out .
```

`--stack` is one of `vapi`, `retell`, `twilio`, `livekit`, `pipecat` -- every
stack hotato has a real, shipped connector for today (see
[`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)). `--out` is usually `.`, the root of
the repo you are adding hotato to. Offline: no network, no credentials needed
to generate.

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

Every file is refused if it already exists, unless `--force` is passed --
nothing is silently merged or overwritten, and nothing partial is left behind
if the scaffold refuses. The generated file names are deliberately namespaced
away from a real repo's own files (`HOTATO.md`, not `README.md`;
`hotato-contracts.yml`, not `hotato.yml`) so a first run does not collide with
files a voice-agent repo almost always already has.

## Two input paths, chosen for you by `--stack`

**Auto-pull** (`vapi`, `retell`, `twilio`): hotato fetches the recording
itself once you connect a key. `hotato.yaml`'s `credentials.env` names the
exact environment variable(s) (`VAPI_API_KEY`; `RETELL_API_KEY`;
`TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`) `hotato connect <stack>` also
reads. `recording.access` is `auto-pull`.

**Capture-in-your-infra** (`livekit`, `pipecat`): there is no vendor
recording API to pull from, so no credentials are generated or needed.
`hotato.yaml`'s `credentials.env` is `[]` and `recording.access` is
`capture-in-your-infra`; `hotato setup --stack <stack>` prints the exact
two-track capture scaffold, and you point `hotato contract create --stereo`
at the WAV your own deployment writes.

## The CI gate

`.github/workflows/hotato-contracts.yml` runs on push, on pull request, and
weekly. It is two guarded steps, both a **no-op, never a failure**, until you
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
(`hotato sweep --stack <stack>`), ranked by hotato's own salience -- never a
verdict, never auto-labeled. It ships **disabled** (`if: false`): flip it to
`true` once the stack's credential env var(s) are set as repo secrets
(Settings -> Secrets and variables -> Actions). Hotato never runs a live pull
against your account on its own initiative; enabling this job is an explicit
human decision, made once, in your own CI config. `livekit`/`pipecat` carry
no such job -- there is no vendor recording API to sweep.

## Turn your first bad call into a contract

```bash
# auto-pull stacks
hotato connect vapi --api-key <key>
hotato sweep --stack vapi --out hotato-sweep.html
# open hotato-sweep.html, pick a real candidate moment, then:
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

## Read more

- The bundle layout and the create/verify/inspect/pack/unpack commands:
  [`CONTRACTS.md`](CONTRACTS.md)
- The underlying fixture primitive, one bad call to a CI gate in five steps:
  [`BAD-CALL-TO-CI.md`](BAD-CALL-TO-CI.md)
- Per-stack connector support, verified against the vendor's live docs:
  [`ADAPTER-STATUS.md`](ADAPTER-STATUS.md)
- The connect-once bulk pull-and-analyze recipe: [`CONNECT.md`](CONNECT.md)
- An agent adding hotato to a repo end to end: [`../AGENTS.md`](../AGENTS.md)
