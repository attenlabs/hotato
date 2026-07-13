# Set and forget: passive turn-taking regression monitoring

`hotato sweep` turns from a command you remember to run into a job that runs
on its own schedule and only asks for your attention when it finds something
real.

Every command below is a shipped `hotato` command (verify with `hotato
<command> --help`). Try the whole loop right now with `--demo`, no
credentials needed, before pointing it at your own stack.

## The loop

```
connect (once) -> sweep (on a schedule) -> read the dashboard
   -> promote confirmed candidates into fixtures -> hotato run gates CI
```

Sweeping never changes anything by itself: it lists candidate timing
moments, never a verdict. You decide which ones are real bugs and label
them; only `hotato fixture create` / `hotato fixture promote` turn a
candidate into a permanent test.

## 1. Connect once

```bash
hotato connect vapi --api-key <key>
# or: VAPI_API_KEY=<key> hotato connect vapi
```

This runs a lightweight live auth check (skip with `--no-verify`) and stores
the credential in `~/.hotato/connections.json`, file mode `0600`. The key is
never sent to Hotato, only to the vendor's own API. Full per-stack
credential table (Vapi, Twilio, Retell, Bland, ElevenLabs, Synthflow, Millis,
Cartesia): [`CONNECT.md`](CONNECT.md). LiveKit and Pipecat are
capture-in-your-infra, not connectable; use `hotato setup --stack
livekit|pipecat` instead.

Once a stack is connected, `--stack` and the credential flags are optional
on `pull` / `sweep` whenever exactly one stack is connected.

## 2. Sweep on a schedule

Run the same command your terminal runs interactively, from cron or CI:

```bash
hotato sweep --stack vapi --since 7d --format json > hotato-sweep.json
hotato sweep --stack vapi --since 7d --out hotato-sweep.html --no-open
```

- `--format json` is the machine-readable candidate list `fixture promote`
  reads; redirect it to a file every time (`--out` only writes the HTML
  dashboard, not the JSON. For JSON, capture stdout).
- `--out FILE.html --no-open` writes the shareable dashboard without popping
  a browser, the right mode when nothing is watching the screen.
- `--since 7d` scopes the pull to recent calls; a nightly job narrows this to
  `--since 1d` so it only re-scores what came in since the last run.

A crontab entry, 03:00 daily:

```
0 3 * * * cd /path/to/repo && hotato sweep --stack vapi --since 1d --format json > "reports/sweep-$(date +\%F).json" && hotato sweep --stack vapi --since 1d --out "reports/sweep-$(date +\%F).html" --no-open
```

See [`examples/set-and-forget/`](../examples/set-and-forget/README.md) for a
runnable version of this, plus the CI half of the loop.

No stack connected yet? Everything above works with `--demo` instead of
`--stack ... --since ...`, credential-less, against two bundled recorded calls:

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato sweep --demo --out hotato-sweep.html --no-open
```

## 3. Read the report

The HTML dashboard (the default `--format`) is one self-contained file:
every candidate moment across every swept call, ranked by salience, with the
hear-the-bug audio player embedded for the top `--audio-top` (default 8) so
you can listen before deciding anything. Calls that could not be scored
(mono/mixed stacks without `--allow-mono`, an unreadable file) list under
Skipped with the reason, a logged skip, not a silent drop.

The JSON (`--format json`) is the same candidate list as structured data.
Each entry carries the source recording, the timestamp (`t_sec`), the kind
(`agent_stop_no_caller`, `overlap_while_agent_talking`, ...), and a salience
score. Nothing in either output is a verdict: a candidate is a timing fact,
not a label. You decide, per candidate, whether the agent should have
yielded or held.

## 4. Promote a confirmed bug into a fixture

Once you have listened to a candidate and decided what should have
happened, `hotato fixture promote` turns it into a permanent regression test
in one command, no `--stereo` / `--onset` needed, since the candidate ref
already carries the recording and the moment:

```bash
hotato fixture promote hotato-sweep.json#3 --expect yield \
    --id refund-cutoff-001 --out tests/hotato
```

`CANDIDATE_REF` is `FILE#N` (the Nth candidate, ranked order, matching the
report's numbering) or `FILE#CALL:N` (the Nth candidate from one call, named
by its source file or pulled call id), for example `hotato-sweep.json#3` or
`hotato-sweep.json#call_abc123:2`. `--expect yield` means the agent should
have stopped for the caller; `--expect hold` means it should have kept
talking through a backchannel. The fixture is scored immediately: a
candidate that turns out not to be scorable is refused (exit 2), never
written as a fixture that would report a meaningless verdict.

This is the most important step in the loop. It is how "this suspicious
moment is real" becomes a fact your CI enforces forever, instead of
something you noticed once and forgot. (`hotato fixture create --stereo ...
--onset ...` does the same thing from a raw recording and a timestamp you
already know, if you are not starting from a sweep/analyze result.)

## 5. Gate CI on your fixtures

Every promoted fixture lives under `--out DIR` (`tests/hotato/scenarios` +
`tests/hotato/audio` above) and scores with the same command whether it runs
on your laptop or in CI:

```bash
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio --format json
```

Exit code `1` means a regression is pinned; `0` means every promoted fixture
still passes. Wire that into a GitHub Action (drop-in workflow at
[`.github/workflows/hotato.yml`](../.github/workflows/hotato.yml), guide at
[`docs/CI.md`](CI.md)) or into an existing pytest run with one flag
(`pytest --hotato-suite --hotato-suite-scenarios tests/hotato/scenarios
--hotato-suite-audio tests/hotato/audio`, see [`docs/PYTEST.md`](PYTEST.md)).
[`examples/set-and-forget/`](../examples/set-and-forget/README.md) has a
complete cron + CI pairing.

## Worked example (zero setup, verified end to end)

Every command above works right now on the bundled demo calls, so you can
see one real failure get pinned before wiring anything to your own stack:

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato fixture promote hotato-sweep.json#2 --expect yield \
    --id demo-missed-interruption --out tests/hotato
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
# exit code 1: the demo agent never yielded for a real interruption -- pinned
```

That last `run` prints the fix card too (fix class, the config knob, the
direction to move it), because the fixture that just failed is a
labelled bad-agent moment from the bundled demo battery, not a placeholder.

## What this does not do

- Hotato never auto-labels a candidate, never auto-creates a fixture, and
  never auto-tunes a threshold. Promotion is always a command you run, for a
  candidate you listened to.
- `sweep` and `ingest` (the webhook-driven version of this same loop, see
  [`docs/INGEST.md`](INGEST.md)) both report candidates, never a pass/fail.
  Only fixtures scored with `hotato run` produce a verdict.
- There is no daemon and no hosted service. Cron, CI, and a webhook handler
  are all just processes that shell out to the same CLI; the schedule is
  yours to own.
