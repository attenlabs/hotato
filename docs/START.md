# Start here: the guided first run

`hotato start --demo` runs the whole hotato loop on bundled data, in two
acts. Act one (timing): it sweeps the two demo recordings (the same calls
`hotato demo` scores) and builds and verifies one failure contract from
them. Act two (say-do): it runs one conversation check over a bundled
scripted conversation -- the agent says the refund was sent; the trace and
the post-call state show it was not issued. It writes every artifact you
need to see both flows and prints the exact next commands.

```bash
hotato start --demo
```

## What it writes

Into the current directory (or `--dir DIR`):

| File | What it is |
|---|---|
| `hotato-sweep.json` | Every candidate timing moment across the two demo calls, ranked. An `analyze` result -- `FILE#N` off it drives `fixture promote` / `card` unchanged. |
| `hotato-sweep.html` | Self-contained dashboard (embedded audio, no external assets), opens in any browser. |
| `hotato-no-single-threshold.svg` | Threshold-funnel card: the demo battery misses an interruption **and** false-stops on a backchannel, so no single dial fixes both. See `docs/CARDS.md`. |
| `contracts/demo-missed-interruption.hotato/` | One failure contract, built from the sweep's missed-interruption candidate with `--expect yield`, verified on the spot. See `docs/CONTRACTS.md`. |
| `saydo/` | Act two's conversation: `transcript.json`, `trace.jsonl`, `state.json`, `test.json`, and the evaluated `test-run.json`. See below. |

Everything runs offline: demo, analyze, card, contract, and say-do steps
all run on packaged audio, packaged conversation files, and local files
alone.

## The demo failure contract

`start --demo` runs the whole loop once, on a bundled recording -- not
just a ranked list of candidates:

```bash
hotato contract create --from-candidate hotato-sweep.json#2 --expect yield \
    --id demo-missed-interruption --out contracts
hotato contract verify contracts/
```

Candidate #2 in the bundled sweep is the missed-interruption call: the
agent talked over the caller instead of yielding. Scored against
`--expect yield`, it fails -- so `start --demo` prints:

```
verified contract: FAIL as expected -- the demo call missed the
interruption; a CI gate on this contract catches any change to the evidence
or policy -- catching the AGENT regressing requires a fresh recapture (see
docs/RECAPTURE.md)
(start --demo itself exits 0 because setup succeeded; run the next command to
see the contract's CI exit 1: hotato contract verify contracts/)
```

`hotato contract verify contracts/` re-scores that same audio and reports
the same regression (exit `1`); a CI job wired to it fails the same way if
the evidence or policy ever changes. Telling whether a currently deployed
agent still has the bug takes a fresh recapture:
[`docs/RECAPTURE.md`](RECAPTURE.md). Two-lane breakdown:
[`docs/CONTRACTS.md`](CONTRACTS.md#two-lanes-what-verify-proves-depends-on-which-recording-you-feed-it).
Run it yourself:

```bash
hotato contract verify contracts/
```

## Act two: the say-do check

Timing is act one -- how the call sounded. Act two checks what the agent
did. The demo bundles one scripted say-do conversation (`saydo/`,
mirroring the reference agent's `refund-claimed-not-issued` job in
`examples/reference-agent`): the caller asks for a refund, the agent looks
the order up, and then says the refund was sent -- while the trace carries
no `issue_refund` tool span and the post-call state's `refund_status`
stays `"none"`. Every packaged file is verified against the sha256 its
manifest records before use.

`start --demo` evaluates that conversation through the same machinery
`hotato test run` drives -- a `phrase` assertion holds the agent's claim
(it passes), and the `tool_result` + `state` assertions read the trace
(Authority 1) and the post-call state (Authority 2) -- so it prints:

```
say-do check:    FAIL, by design: the agent said the refund was sent;
                 the trace shows no such tool call succeeded (no
                 issue_refund span), and the order's post-call
                 refund_status stayed "none".
```

Tool and state evidence decide the outcome, never the agent's words. The
evaluated result lands at `saydo/test-run.json`; render it as the say-do
card (`docs/CARDS.md`) with:

```bash
hotato card saydo/test-run.json --out saydo-card.svg
```

Replay the check as the CI gate it is (exit `1`, by design):

```bash
hotato test run saydo/test.json --agent demo-agent \
    --transcript saydo/transcript.json --trace saydo/trace.jsonl \
    --state saydo/state.json
```

The whole conversation-QA loop behind it -- suites, scenarios, the 375-run
reference agent -- lives in [`docs/CONVERSATION-TEST.md`](CONVERSATION-TEST.md)
and [`docs/SUITE-RUN.md`](SUITE-RUN.md).

## What it prints

The exact next commands, ready to copy:

1. **Save a candidate as a permanent regression test** -- you choose the
   label, hotato leaves that judgment to you:

   ```bash
   hotato fixture promote hotato-sweep.json#1 --expect <yield|hold> \
       --id my-first-fixture --out tests/hotato
   ```

2. **Run your fixtures in CI** (exits non-zero on a regression):

   ```bash
   hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio
   ```

3. **Render a PR card** from any candidate:

   ```bash
   hotato card hotato-sweep.json#1 --out candidate.svg
   ```

4. **Re-verify the demo failure contract in CI** (or build your own from any
   candidate with `hotato contract create`):

   ```bash
   hotato contract verify contracts/
   ```

5. **Replay the say-do gate** (exit `1`, by design) and **render the
   say-do card**:

   ```bash
   hotato test run saydo/test.json --agent demo-agent \
       --transcript saydo/transcript.json --trace saydo/trace.jsonl \
       --state saydo/state.json
   hotato card saydo/test-run.json --out saydo-card.svg
   ```

## Other modes

`--stack`, `--folder`, and `--stereo` each hand you the shipped command
for the job:

| Flag | Command | Does |
|---|---|---|
| `--stack` | `hotato sweep --stack <stack>` | Connect once, sweep your own calls |
| `--folder` | `hotato analyze <folder>` | Scan a directory of recordings |
| `--stereo` | `hotato run --stereo <call.wav>` | Score one dual-channel call |

## Output and exit codes

`--format json` emits a machine object (the files written, the next
commands, a `contract` block with the demo contract's id, expect,
scorable, passed, and `verified_fail_as_expected`, and a `saydo` block
with the say-do check's test id, exit code, claim assertion, and evidence
assertions with their share-safe public reasons) for an agent to drive.

- **0**: the guided first run completed -- including when `--stack`,
  `--folder`, or `--stereo` printed the shipped command to use instead.
  This holds even though the demo contract FAILS its policy and the
  say-do check FAILS its test: `start --demo` finishing is separate from
  those gates passing, which `hotato contract verify contracts/` and
  `hotato test run saydo/test.json ...` each report (exit `1`, by
  design).
- **2**: usage error -- no mode given, or `--dir` is not a directory.
