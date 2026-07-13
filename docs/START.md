# Start here: the guided first run

`hotato start --demo` is the zero-setup first run: one command, on your
machine. It sweeps the two bundled demo calls (the same recordings `hotato
demo` scores), creates and verifies one failure contract from them, writes
everything you need to see the flow, and then prints the exact next
commands.

```bash
hotato start --demo
```

## What it writes

Into the current directory (or `--dir DIR`):

- `hotato-sweep.json` -- the sweep result: every candidate timing moment across
  the two demo calls, ranked. This is an `analyze` result, so a `FILE#N` ref
  off it drives `hotato fixture promote` and `hotato card` unchanged.
- `hotato-sweep.html` -- a self-contained dashboard (embedded audio, no external
  assets) you can open in any browser.
- `hotato-no-single-threshold.svg` -- the threshold-funnel card: the demo battery
  misses an interruption **and** false-stops on a backchannel, so no single
  dial fixes both. See `docs/CARDS.md`.
- `contracts/demo-missed-interruption.hotato/` -- one failure contract,
  created from the sweep's missed-interruption candidate with `--expect
  yield` and verified immediately. See `docs/CONTRACTS.md`.

Everything is offline by construction: the demo pulls from packaged audio, and
the analyze/card/contract steps run on packaged audio and local files alone.

## The demo failure contract

`start --demo` goes beyond a ranked list of candidates: it runs the whole
loop once, on one of the bundled recordings:

```bash
hotato contract create --from-candidate hotato-sweep.json#2 --expect yield \
    --id demo-missed-interruption --out contracts
hotato contract verify contracts/
```

Candidate #2 in the bundled sweep is the missed-interruption call: the
agent talked over the caller instead of yielding. Scored against `--expect
yield`, it fails -- so `start --demo` prints:

```
verified contract: FAIL as expected -- the demo call missed the
interruption; a CI gate on this contract catches any change to the evidence
or policy -- catching the AGENT regressing requires a fresh recapture (see
docs/RECAPTURE.md)
(start --demo itself exits 0 because setup succeeded; run the next command to
see the contract's CI exit 1: hotato contract verify contracts/)
```

`hotato contract verify contracts/` re-scores the SAME bundled audio and
reports the SAME regression (exit code `1`), and a CI job wired to it fails
the same way if that bundled evidence or policy ever changes. On this frozen
recording, telling you whether a CURRENT deployed agent still has the bug
takes a fresh capture -- re-running the same caller stimulus against it and
verifying that; see
[`docs/RECAPTURE.md`](RECAPTURE.md). See [`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it)
for the full two-lane breakdown. Run it yourself:

```bash
hotato contract verify contracts/
```

## What it prints

The exact next commands, ready to copy:

1. **Save a candidate as a permanent regression test** (you choose the label --
   Hotato leaves that judgment to you):

   ```bash
   hotato fixture promote hotato-sweep.json#1 --expect <yield|hold> \
       --id my-first-fixture --out tests/hotato
   ```

2. **Run your fixtures in CI** (exits non-zero on a regression):

   ```bash
   hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
   ```

3. **Render a shareable card** from any candidate:

   ```bash
   hotato card hotato-sweep.json#1 --out candidate.svg
   ```

4. **Re-verify the demo failure contract in CI** (or create your own from any
   candidate with `hotato contract create`):

   ```bash
   hotato contract verify contracts/
   ```

## Other modes

`--stack`, `--folder`, and `--stereo` are placeholders in this build. They print
the shipped command that does the job today:

- `--stack` -> `hotato sweep --stack <stack>` (connect once, sweep your own
  calls).
- `--folder` -> `hotato analyze <folder>` (scan a directory of recordings).
- `--stereo` -> `hotato run --stereo <call.wav>` (score one dual-channel call).

## Output and exit codes

`--format json` emits a machine object (the files written, the next commands,
and a `contract` block with the demo contract's id, expect, scorable, passed,
and `verified_fail_as_expected`) for an agent to drive.

- **0**: the guided first run completed (or a stubbed mode printed the shipped
  command to use instead). This holds even though the demo contract itself
  FAILS its policy -- `start --demo` finishing is a separate claim from the
  demo contract passing; `hotato contract verify contracts/` is the command
  that reports that (exit `1`, by design).
- **2**: usage error: no mode given, or `--dir` is not a directory.
