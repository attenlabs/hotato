# Set and forget: passive turn-taking regression monitoring

`hotato sweep` turns from a command you remember to run into a job that
runs on schedule and asks for your attention only when it finds something
worth acting on.

Every command below is shipped (verify with `hotato <command> --help`).
Run the whole loop now with `--demo` -- no credentials needed -- before
pointing it at your own stack.

## The loop

```
connect (once) -> sweep (on a schedule) -> read the dashboard
   -> promote confirmed candidates into fixtures -> hotato run gates CI
```

Sweeping only reads: it lists candidate timing moments for you to judge
and label. `hotato fixture create` / `hotato fixture promote` turn a
candidate into a permanent test.

## 1. Connect once

```bash
hotato connect vapi --api-key <key>
# or: VAPI_API_KEY=<key> hotato connect vapi
```

Runs a lightweight live auth check (skip with `--no-verify`) and stores
the credential in `~/.hotato/connections.json`, file mode `0600`. The key
travels only to the vendor's own API, kept out of hotato's hands. Full
per-stack credential table: [`CONNECT.md`](CONNECT.md). LiveKit and
Pipecat are capture-in-your-infra: use `hotato setup --stack
livekit|pipecat` instead.

Once connected, `--stack` and credential flags are optional on `pull` /
`sweep` if exactly one stack is connected.

## 2. Sweep on a schedule

Run the same command your terminal runs interactively, from cron or CI:

```bash
hotato sweep --stack vapi --since 7d --format json > hotato-sweep.json
hotato sweep --stack vapi --since 7d --out hotato-sweep.html --no-open
```

| Flag | Does |
|---|---|
| `--format json` | Machine-readable candidate list `fixture promote` reads -- redirect to a file every time (`--out` writes the HTML dashboard; capture stdout for the JSON) |
| `--out FILE.html --no-open` | Writes the shareable dashboard without popping a browser -- for when nothing is watching the screen |
| `--since 7d` | Scopes the pull to recent calls; a nightly job narrows this to `--since 1d` so it only re-scores what's new |

A crontab entry, 03:00 daily:

```
0 3 * * * cd /path/to/repo && hotato sweep --stack vapi --since 1d --format json > "reports/sweep-$(date +\%F).json" && hotato sweep --stack vapi --since 1d --out "reports/sweep-$(date +\%F).html" --no-open
```

A runnable version of this, plus the CI half of the loop:
[`examples/set-and-forget/`](../examples/set-and-forget/README.md).

No stack connected? Everything above works with `--demo` instead of
`--stack ... --since ...` -- credential-free, on two bundled calls:

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato sweep --demo --out hotato-sweep.html --no-open
```

## 3. Read the report

| Output | What it is |
|---|---|
| HTML (default `--format`) | One self-contained file: every candidate across every swept call, ranked by salience, hear-the-bug audio embedded for the top `--audio-top` (default 8). Calls that couldn't be scored (mono/mixed without `--allow-mono`, an unreadable file) list under Skipped with the reason. |
| JSON (`--format json`) | The same list as structured data: source recording, timestamp (`t_sec`), kind (`agent_stop_no_caller`, `overlap_while_agent_talking`, ...), salience score -- a fact for you to judge, per candidate: yielded, or held? |

## 4. Promote a confirmed bug into a fixture

Once you've listened to a candidate and decided what should have happened,
`hotato fixture promote` turns it into a permanent regression test -- no
`--stereo` / `--onset` needed, since the candidate ref already carries the
recording and the moment:

```bash
hotato fixture promote hotato-sweep.json#3 --expect yield \
    --id refund-cutoff-001 --out tests/hotato
```

`CANDIDATE_REF` is `FILE#N` (Nth candidate, ranked order) or
`FILE#CALL:N` (Nth candidate from one call, by file or call id) -- e.g.
`hotato-sweep.json#3` or `hotato-sweep.json#call_abc123:2`. `--expect
yield`: the agent should have stopped. `--expect hold`: it should have
kept talking through a backchannel. Scored immediately -- an unscorable
candidate is refused (exit 2).

This is the loop's most important step: a suspicious call becomes a fact
CI enforces forever, not something you noticed once and forgot. (`hotato
fixture create --stereo ... --onset ...` does the same from a raw
recording and a timestamp you already know.)

## 5. Gate CI on your fixtures

Every promoted fixture lives under `--out DIR` (`tests/hotato/scenarios` +
`tests/hotato/audio` above) and scores with the same command, laptop or
CI:

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio --format json
```

Exit `1` pins a regression; `0` means every fixture passes. Wire it into a
GitHub Action (drop-in workflow: [`.github/workflows/hotato.yml`](../.github/workflows/hotato.yml),
guide: [`docs/CI.md`](CI.md)) or an existing pytest run with one flag
(`pytest --hotato-suite --hotato-suite-scenarios tests/hotato/scenarios
--hotato-suite-audio tests/hotato/audio`, see [`docs/PYTEST.md`](PYTEST.md)).
[`examples/set-and-forget/`](../examples/set-and-forget/README.md) has the
complete cron + CI pairing.

## Worked example (zero setup, verified end to end)

Every command above works right now on the bundled demo calls -- watch a
failure get pinned before wiring anything to your own stack:

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato fixture promote hotato-sweep.json#2 --expect yield \
    --id demo-missed-interruption --out tests/hotato
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
# exit code 1: the demo agent never yielded for an interruption -- pinned
```

That last `run` also prints the fix card (fix class, config knob,
direction to move it): the failed fixture is a labelled bad-agent moment
from the bundled demo battery.

## What you control in this loop

- Labeling, fixture creation, and threshold tuning are each a command you
  run, for a candidate you listened to.
- `sweep` and `ingest` (the webhook-driven version of this loop, see
  [`docs/INGEST.md`](INGEST.md)) both report candidates as timing facts;
  fixtures scored with `hotato run` produce the pass/fail verdict.
- This runs as processes you control: cron, CI, and a webhook handler all
  shell out to the same CLI, on a schedule that's yours to own.
