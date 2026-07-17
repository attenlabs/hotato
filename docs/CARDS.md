# Cards: one measured moment, rendered for the pull request

`hotato card` turns a machine result into a self-contained SVG for a pull
request or an issue. The card names the measured timing moment: a
reproducible measurement, not a verdict about intent, with no accuracy
number and no blended score anywhere on it. The PR-native artifact the
shipped Action lands automatically is the
[Failure Record](#the-failure-record-the-artifact-that-lands-in-the-pull-request)
below; a card is the single-moment image you attach yourself.

```bash
hotato card INPUT[#REF] --out card.svg
```

Everything runs locally. The SVG is a pure function of the input JSON alone
(the same input renders the same bytes forever), with every color inlined
and no font, image, stylesheet, script, or link to fetch. It renders
wherever the PR renders; no CDN or asset host required.

## The cards, auto-detected

The input's kind decides the card; you do not pick.

| Input | Card |
|---|---|
| a `sweep`/`analyze` candidate ref, `FILE#N`, that is a talk-over moment | **talk-over candidate** |
| a `sweep`/`analyze` candidate ref, `FILE#N`, that is a false-stop moment | **false-stop candidate** |
| a fix plan whose `decision` is `do_not_tune_single_threshold` | **threshold funnel** (the hero) |
| a supported `hotato verify` before/after rollup that improved | **paired comparison** |
| a `hotato contract create` contract (kind `voice-turn-taking-contract`) | **failure contract** |
| a `hotato test run` result (kind `hotato.test-run`) whose tool/state evidence failed a declared outcome | **say-do failure** |

`#N` is the same 1-based rank the sweep report and dashboard show, and the
same ref `hotato fixture promote` takes -- a card and a fixture speak of
the exact same moment.

### A. Talk-over candidate

An `overlap_while_agent_talking` (or `agent_start_during_caller`) moment:
the agent kept the floor while the caller was speaking. The card leads with
the measured overlap in seconds and closes with "Hotato reports timing
candidates, not intent."

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato card hotato-sweep.json#3 --out talk-over.svg
```

### B. False-stop candidate

An `agent_stop_no_caller` moment: the agent went quiet with no caller
nearby to explain the drop. The card leads with the measured trailing
silence.

```bash
hotato card hotato-sweep.json#1 --out false-stop.svg
```

### C. Threshold funnel (the hero)

The plan the both-axes case produces: the battery missed an interruption
**and** false-stopped on a backchannel, so satisfying both needs more than
a single sensitivity dial. The card states Hotato refused threshold
tuning and names the fix class (`engagement-control`) -- the card the
project leads with.

```bash
hotato demo --format json > demo.json
hotato plan demo.json --out fix-plan.json
hotato card fix-plan.json --out no-single-threshold.svg
```

Only a `do_not_tune_single_threshold` plan renders this card; any other
plan is a clean exit-2 usage error (not one of the card kinds).

### D. Paired comparison

A supported `hotato verify` before/after rollup where at least one
previously-failing fixture now passes and no hold/backchannel fixture
regressed. The card reads "PAIRED FRESH-RECAPTURE IMPROVED" only when the
recapture is runner-attested and "PAIRED (OPERATOR-ASSERTED)" otherwise,
never "verified" or "fix verified", and closes with "Hotato reports
coincidence, not causation." A verify result that doesn't support that
claim (too few previously-failing fixtures, nothing now passing, or a
regressed hold fixture) is refused with exit 2.

```bash
hotato card verify.json --out comparison.svg
```

### E. Failure contract

A committed contract's card: the expected behavior (`yield` or `hold`), the
measured seconds, and PASSED / FAILED / NOT SCORABLE. The footer reads "A
human labeled this contract; Hotato measured the timing."

```bash
hotato card contracts/demo-missed-interruption.hotato/contract.json --out contract.svg
```

### F. Say-do failure

A `hotato test run` result (`--format json`, kind `hotato.test-run`) whose
deterministic lane failed a tool/state evidence assertion (`tool_result`,
`tool_call`, `tool_error`, `http_result`, `state`, `state_change`): the
conversation claims an outcome the trace (Authority 1) or the post-call
state (Authority 2) does not back. The card renders the claim vs the
evidence -- the failing assertion's id and kind, its span refs when the
evaluator recorded any, and its share-safe `public_reason` (built from
allowlisted structured fields only, never transcript text, a tool payload,
or a state value). The failing outcome-tagged evidence assertion leads;
the footer reads "Tool and state evidence decide the outcome, never the
agent's words." A test-run result with no failing tool/state evidence
assertion is refused with exit 2.

```bash
hotato start --demo   # act two writes saydo/test-run.json
hotato card saydo/test-run.json --out saydo-card.svg

# or from any of your own runs:
hotato test run refund.yaml --agent support-v3 --trace voice_trace.jsonl \
    --transcript call.transcript.json --format json > test-run.json
hotato card test-run.json --out saydo-card.svg
```

## Redaction: identifiers stay hidden by default

A card is a public image, so identifiers stay hidden by default: call id,
filesystem path (only a basename is ever shown), and vendor recording name
are omitted. A pulled recording named `STACK__ID.wav`
carries the call id inside its name; that name shows only under
`--include-identifiers`.

```bash
# shows the source recording's basename on a candidate card
hotato card hotato-sweep.json#1 --out card.svg --include-identifiers
```

## The Failure Record: the artifact that lands in the pull request

A card is one moment's image you attach yourself. The **Failure Record**
is the PR-native trust artifact: the shipped GitHub Action renders one per
non-passing unit ([`docs/CI.md`](CI.md)), so it arrives in the pull
request with the run that produced it. Each record is a lane-structured
projection of one failed, inconclusive, or errored result:

- one evidence-specific headline;
- the five lanes (outcome, policy, conversation, speech, reliability),
  each with its own status and never a blended or overall score;
- the reproduce command (`reproduction.argv`, rendered in the Markdown,
  HTML, and SVG forms) plus the pinned one-command verifier
  (`hotato record verify failure-record.json`);
- content-addressed evidence digests and a privacy profile: no audio,
  transcript body, tool payload, state value, or absolute path, so it
  attaches to a PR as-is.

`hotato start --demo` emits one automatically under
`hotato-failure-record/` --
`failure-record.{json,md,html,svg}` -- alongside the sweep and the demo
contract, and prints the record paths plus the one-command verifier.
Render one from any result yourself with `hotato record render`.

## Output and exit codes

Without `--out`, the SVG goes to stdout, so you can pipe it. With `--out`
it is written there atomically.

- **0**: the SVG card was rendered (to `--out`, or to stdout).
- **2**: usage error, unreadable input, a bad candidate ref, or an input
  not a fix plan / verify result / contract / test-run result / sweep
  candidate.

## Regenerating the committed cards

Four commit-ready examples live under `docs/assets/cards/`
(`no-single-threshold-card.svg`, `talk-over-card.svg`,
`false-stop-card.svg`, `say-do-card.svg`), rendered from the bundled demo
data. Regenerate them with:

```bash
PYTHONPATH=src python3 scripts/render_card_assets.py
```
