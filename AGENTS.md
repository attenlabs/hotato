# AGENTS.md

You are an agent evaluating or wiring up **hotato** for a voice agent. This file
is written to be executed top to bottom. Every command below is offline,
reversible, and needs no account or key. Prefer the CLI's own contract
(`hotato describe --format json`) over this prose when they disagree.

## What it is

hotato turns a failed voice call into a deterministic regression test that lives
in your repo and reproduces forever: self-hosted conversation QA that measures
the timing between the two voices of a recorded call and locks each catch into a
content-addressed CI contract with a stable pass/fail exit. Give it both channels
of a recorded call (caller on one channel, agent on the other) and it measures the
timing between the two voices: did the agent stop talking when the caller took
the floor (a yield), how many seconds that took, and how many seconds both were
talking at once (talk-over). It runs on the machine that invokes it and never
sits in the production audio path.

The name is hot potato. A turn in a conversation is one: hold it a beat too long
and you have dropped it. hotato measures how fast your agent passes it back, and
whether it wrongly stops for a backchannel it should have talked through.

## Precondition (check this first)

Scoring needs two separate channels. A mono or mixed export is marked NOT
SCORABLE (exit 2), not scored. If your provider only exports mono, that is the
one real blocker; `hotato setup --stack <name>` prints the dual-channel capture
config. Confirm a file is scorable before scoring it:

```bash
hotato trust --stereo call.wav        # per-channel activity, swap flag, scorability
```

## Try it in 10 seconds (no credentials)

```bash
uvx hotato demo --fail                 # zero-install; scores the bundled battery
# no uv on this box:
pipx run hotato demo --fail
# or: pip install hotato && python -m hotato demo --fail
```

It scores two recorded calls a provider's default agent got wrong, prints each
caught moment (`did_yield`, `seconds_to_yield`, `talk_over`), writes an HTML
report to a temp path, and **exits 1** because both fail on timing. Exit 1 is the
signal, not an error. `hotato demo` without `--fail` runs the same battery and
exits 0.

## Three ways in (friction order)

Pick the onramp matching the data the project already has; every path feeds
the same offline scoring and the same 0/1/2 exit contract.

1. **Traces, no audio needed.** `tool_call` assertions read only the ingested
   trace's `voice_trace.v1` spans; `outcome` assertions combine those spans
   with transcript phrases: say-do verification that what the agent said
   matches what the backend did, deterministic end to end.

   ```bash
   hotato trace ingest --otel traces.jsonl --out voice_trace.jsonl
   hotato assert init --from-trace voice_trace.jsonl   # starter assertions.yaml from the spans
   hotato assert run --trace voice_trace.jsonl --transcript call.transcript.json --assertions assertions.yaml
   ```

2. **The stack's recorded calls.** After `hotato connect <stack>` (or the key
   in the environment), bulk-fetch recent recordings into a local folder.
   Vapi, Twilio, and Retell fetch a separated two-channel file; Retell has no
   verified list endpoint, so pull it by explicit `--call-id`.

   ```bash
   hotato pull --stack vapi --limit 10
   ```

3. **Scripted fixtures.** A deterministic scripted caller renders a
   `scenario.v1` into `origin=simulated` conversation artifacts; a seeded
   replay is byte-identical, so regression fixtures need no production audio.

   ```bash
   hotato simulate --init demo.scenario.json && hotato simulate demo.scenario.json --out ./sim
   ```

After a first catch on a captured call, the second move is driving a call
against the live agent on demand (the same pull -> score pipeline; it bills
one outbound phone call per run): `docs/DRIVE-A-CALL.md`.

## Is it worth wiring in?

- **Transcript and text tests do not see timing.** A clean transcript can come
  from an agent that talked over the caller for two seconds before it yielded.
  That failure lives in the audio timeline, between the two voices, where a
  text-level eval cannot reach it.
- **The measurement is checkable against human labels.** On 13 recorded clips
  from the AMI Meeting Corpus, the median error between the scorer's measured
  caller-onset and the human word-alignment label is 20 ms. Re-run it yourself
  (see "Verify it yourself"); the report is a per-signal error distribution and a
  yield/hold confusion matrix, with no accuracy percentage anywhere.
- **It gates CI deterministically.** Same audio in, same numbers out, exit 0
  pass / 1 regression. A caught moment becomes a portable contract, so it stays
  caught across releases.
- **MIT, self-hosted, offline.** The scoring core is stdlib-only and makes zero
  network calls; nothing is uploaded, no account, no key. Network is confined to
  opt-in stack-pull paths you invoke deliberately.
- **Scope is fixed by construction.** It measures timing, not intent. It
  surfaces candidate moments; a person labels each one yield (should have
  stopped) or hold (a backchannel to talk through). It never claims what the
  agent meant.

### Which part answers your question

- Turn-taking / barge-in / talk-over, day one: `demo`, `scan`, `run` work
  offline now on a two-channel recording.
- Latency / response-gap: the same commands, the speech signals in the envelope.
- Answer-correctness / task-completion / policy disclosures: you supply the
  transcript, state, labels, or a local judge; these are not measured
  automatically from audio.
- Mono-only capture: NOT SCORABLE (exit 2) until you export both channels.

## Integrate

Scaffold a CI gate into an existing repo in one offline command:

```bash
hotato init starter --stack {vapi,retell,twilio,livekit,pipecat} --out .
```

It writes `hotato.yaml`, `fixtures/`, `contracts/`, `reports/`, a weekly GitHub
Action, and a `HOTATO.md` with the exact next commands. Read `HOTATO.md`.

CI gate one-liner (fails the job on a timing regression):

```bash
hotato contract verify contracts/ --junit hotato.xml
```

**Exit-code contract** for the scoring commands (gate on this, do not parse
stdout):

- `0` every scorable event passed
- `1` a scorable event regressed
- `2` usage error or unusable input (bad flags, corrupt file, mono recording, or
  a well-formed input with no scorable event)

## Verify it yourself

Do not take the numbers on trust; regenerate them.

```bash
# tests: the benchmark harness is pinned by its own suite
PYTHONPATH=src python3 -m pytest tests/test_benchmark.py -q

# re-run the measurement-error benchmark on the recorded AMI clips
PYTHONPATH=src python3 -m hotato.benchmark \
  --scenarios corpus/real/scenarios --audio corpus/real/audio

# and on the deterministic synthetic floor (byte-identical re-runs)
python3 examples/render_examples.py
PYTHONPATH=src python3 -m hotato.benchmark
```

The AMI run reports the per-signal error table (median caller-onset error 20 ms,
n=13) and the yield/hold confusion matrix. `corpus/real/README.md` documents the
provenance chain (CC BY 4.0 source, sha256-pinned, human word alignments as
ground truth) and the caveats, including the backchannel clips where a human
micro-pause reads as a yield.

## Machine surfaces

- `hotato describe --format json` emits every subcommand, its flags, its exit
  codes, the schema URLs, and the version, generated from the CLI's own argparse
  so it cannot drift. Read this before you script anything; do not hardcode the
  version or the command list.
- Schemas ship in-package at `src/hotato/schema/*.v1.json` (for example
  `envelope.v1.json`, `error.v1.json`). Validate the `--format json` output
  offline against these; the `schema_version` and `tool` fields in the envelope
  map to the matching file.
- `llms.txt` and `llms-full.txt` are the machine index; `llms-full.txt` inlines
  every doc for a single-fetch context dump.
- MCP (local stdio): `uvx --from "hotato[mcp]" hotato-mcp` exposes the
  `voice_eval_run` scorer and read/verify/propose fleet tools. Note the footgun:
  `uvx hotato-mcp` without `--from` fails. Configs: `docs/MCP.md`.

## Scope boundaries (properties of how it works)

- Reads audio energy over time only: no speaker identification, no
  transcription, no emotion detection.
- Measures timing, not intent: it surfaces candidate moments; a person supplies
  every yield/hold label. Do not write code, comments, or PR text that states
  what the agent "meant" or "tried to do."
- Read-only over recordings: it never changes a live agent's settings and never
  auto-applies a fix. `hotato apply` is clone-only (a new staging assistant),
  never a production write.
- Never upload or pull customer audio without explicit human consent. The demo,
  scan, benchmark, and contract paths need none of it.
