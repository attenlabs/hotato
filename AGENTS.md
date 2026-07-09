# AGENTS.md

Guidance for a coding agent adding Hotato to a voice-agent repository. If you are
a human, [`README.md`](README.md) is the faster read; this file is the safe recipe
and the rules for an autonomous agent.

Hotato is an offline turn-taking regression tester for voice agents. It scores a
recorded call and measures whether the agent stopped talking when the caller
started (a yield), how long that took, and how many seconds both were talking at
once (talk-over). It runs on the machine that invokes it and never sits in the
production audio path.

## The machine contract

- **Discover every command from the CLI itself, not from prose.** `hotato describe
  --format json` emits every subcommand, its flags, its exit codes, the schema
  URLs, and the version, generated from the CLI's own argparse structure so it
  cannot drift. Read that before you script anything.
- **Exit codes are the signal.** For scoring commands: `0` = every scorable event
  passed, `1` = a scorable event regressed, `2` = usage error or unusable input
  (bad flags, a corrupt file, a mono recording with no scorable events). Gate CI on
  the exit code; do not parse stdout to decide pass or fail.
- **`--format json`** on the scoring commands emits a machine envelope
  (`https://hotato.dev/schema/envelope.v1.json`); errors follow
  `error.v1.json`. Prefer these over the human text output.
- **MCP:** `uvx --from "hotato[mcp]" hotato-mcp` exposes one tool, `voice_eval_run`,
  for driving a score mid-task. Client configs: [`docs/MCP.md`](docs/MCP.md).
- Deeper machine index: [`llms.txt`](llms.txt) and [`llms-full.txt`](llms-full.txt).

## Recipe: add Hotato to a voice-agent repo

Fast path: `hotato init starter --stack {vapi,retell,twilio,livekit,pipecat}
--out .` scaffolds step 2's directories and step 5's CI gate in one offline
command (a stack-tuned `hotato.yaml`, `fixtures/`, `contracts/`, `reports/`,
and `.github/workflows/hotato-contracts.yml`). Read the generated `HOTATO.md`
for the exact next commands; full detail: [`docs/STARTER.md`](docs/STARTER.md).
The steps below are the same recipe done by hand.

Do these in order. Every step is offline and reversible.

1. **Prove it runs, with no credentials.** `uvx hotato sweep --demo --out
   hotato-sweep.html` (or `uvx hotato start --demo`). This sweeps two bundled
   recorded calls a provider's default agent failed and writes the dashboard. No
   account, no keys, no network. If this fails, stop and report; do not proceed.

2. **Create `tests/hotato/`** for the repo's own regression fixtures:
   `tests/hotato/scenarios/` for the label JSON and `tests/hotato/audio/` for the
   dual-channel WAVs. Commit this structure empty; humans fill it (step 4).

3. **Score a recording the repo already has**, if any two-channel WAV is checked in
   or reachable without pulling customer data: `uvx hotato scan --stereo call.wav`.
   Check it is scorable first with `hotato trust call.wav`. A mono file is marked
   NOT SCORABLE; do not try to make it scorable by mixing or faking a channel.

4. **Hand candidate moments to a human to label.** A sweep or scan surfaces
   *candidate* moments with measured timing. It does not decide what the agent
   should have done. A person labels each one `yield` (the agent should have
   stopped for the caller) or `hold` (the caller event was a backchannel; the agent
   should keep the floor), then promotes it:
   `hotato fixture promote hotato-sweep.json#1 --expect yield --out tests/hotato`.
   You may draft the command and the diff; you must not invent the label.

5. **Wire a weekly CI gate.** Add a scheduled GitHub Action (weekly `cron`) that
   runs the committed fixtures:
   `hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio`.
   The job fails (exit `1`) when a fixture's timing regresses. Pattern and pytest
   plugin: [`docs/CI.md`](docs/CI.md) · [`docs/PYTEST.md`](docs/PYTEST.md).

6. **Prove a fix before closing it.** When someone changes turn-taking config, run
   the battery before and after and compare:
   `hotato verify --before before.json --after after.json`. It reports what moved
   across the whole battery (coincidence, not causation); it does not certify a
   root cause.

## Rules an agent must not break

- **Never upload customer audio, and never pull it, without explicit human
  consent.** Connecting a stack (`hotato connect`) and pulling recordings
  (`hotato sweep --stack`, `hotato pull`) touch customer data. Do not run them on
  your own initiative. The demo path needs none of this.
- **Never claim intent.** Hotato measures timing. It surfaces candidate moments; a
  human decides `yield` vs `hold`. Do not write code, comments, or PR text that
  states what the agent "meant" or "tried to do."
- **Never mutate production.** Hotato is read-only over recordings and never changes
  a live agent's settings. There is no apply command. A fix plan is a proposal a
  human reviews and applies by hand.
- **No mono-first.** Scoring needs the caller and agent on separate channels. If
  only a mixed mono track exists, report NOT SCORABLE and ask for a dual-channel
  recording config (`hotato setup --stack <name>` prints it). Do not fabricate a
  second channel.
- **Keep credentials in secrets.** Any stack key belongs in the CI provider's
  secret store and is read from the environment. `hotato connect` stores keys
  `0600`, local only; never commit a key, a token, or a raw recording.
- **Do not claim an accuracy number.** There is none anywhere in Hotato. Every
  verdict is a reproducible timing quoted with the command that produced it. Do not
  summarize results as a percentage or a score.

## Read more

- The one-command starter kit: [`docs/STARTER.md`](docs/STARTER.md)
- The loop, end to end: [`docs/SET-AND-FORGET.md`](docs/SET-AND-FORGET.md) ·
  [`docs/BAD-CALL-TO-CI.md`](docs/BAD-CALL-TO-CI.md)
- What it measures: [`METHODOLOGY.md`](METHODOLOGY.md) · API [`docs/API.md`](docs/API.md)
- Security and data handling: [`SECURITY.md`](SECURITY.md)
