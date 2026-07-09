# Start here: the guided first run

`hotato start --demo` is the zero-setup first run. One command, no account, no
network, no credentials. It sweeps the two bundled real demo calls (the same
recordings `hotato demo` scores), writes everything you need to see the flow, and
then prints the exact next commands.

```bash
hotato start --demo
```

## What it writes

Into the current directory (or `--dir DIR`):

- `hotato-sweep.json` -- the sweep result: every candidate timing moment across
  the two demo calls, ranked. This is a real `analyze` result, so a `FILE#N` ref
  off it drives `hotato fixture promote` and `hotato card` unchanged.
- `hotato-sweep.html` -- a self-contained dashboard (embedded audio, no external
  assets) you can open in any browser.
- `hotato-no-single-threshold.svg` -- the threshold-funnel card: the demo battery
  misses a real interruption **and** false-stops on a backchannel, so no single
  dial fixes both. See `docs/CARDS.md`.

Everything is offline by construction: the demo pulls from packaged audio, and
the analyze and card steps touch no network and read no credential.

## What it prints

The exact next commands, ready to copy:

1. **Save a candidate as a permanent regression test** (you choose the label --
   Hotato never infers intent):

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

## Other modes

`--stack`, `--folder`, and `--stereo` are placeholders in this build. They print
the shipped command that does the job today rather than pretend:

- `--stack` -> `hotato sweep --stack <stack>` (connect once, sweep your real
  calls).
- `--folder` -> `hotato analyze <folder>` (scan a directory of recordings).
- `--stereo` -> `hotato run --stereo <call.wav>` (score one dual-channel call).

## Output and exit codes

`--format json` emits a machine object (the files written and the next commands)
for an agent to drive.

- **0**: the guided first run completed (or a stubbed mode printed the shipped
  command to use instead).
- **2**: usage error: no mode given, or `--dir` is not a directory.
