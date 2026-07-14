# `hotato release compare`: diff two releases, per dimension

`hotato release compare BASELINE CANDIDATE` reads two releases from the fleet
registry and reports what moved between them -- `BASELINE` first, `CANDIDATE`
second. It reports movement only; gating a release on that movement is a
separate step (a `required_for_release` suite).

Each release is a snapshot recorded by [`hotato suite run`](SUITE-RUN.md).
Run the same suite twice under two stable `--release` ids in the same
workspace and registry, then diff them.

## What it reports

- **Per-dimension counts for each side, plus the delta.** Each dimension
  (`outcome`, `policy`, `conversation`, `speech`, `reliability`) keeps its
  own three counts (pass / fail / inconclusive) and its own delta -- every
  dimension scored on its own lane.
- **New failures** -- a `scenario x dimension` that PASSED on the baseline
  and FAILs on the candidate. **Fixed-since** -- the reverse. Both diff
  **only** where BOTH releases ran the same `scenario x dimension`; a
  scenario only one side ran counts as new coverage, separate from a
  regression.
- **Per-scenario status changes** -- every `scenario x dimension` whose
  status differs between the two, diffed only where both sides have a
  comparable result.

The releases' pinned digests are surfaced, so the reader knows exactly
which two snapshots were compared, every time.

## Empty state

A side with no runs is stated plainly:

- a release id absent from the workspace is reported as unregistered;
- a release that is registered but has **zero runs** is reported as an
  empty state.

When no `scenario x dimension` result is comparable across both releases,
the report says so: new-failures / fixed-since needs a scenario BOTH
releases ran.

## Compare

```bash
hotato release compare reference-agent-v1 reference-agent-rc2
```

`--workspace`/`-w ID` selects the fleet-registry workspace (default `default`);
`--registry PATH` sets the registry home (default `~/.hotato/fleet`);
`--format {text,json}` picks the output.

## Worked example: two reference-agent releases

Record two releases of the reference agent into the same registry and
workspace, then diff them. The reference example writes to
`examples/reference-agent/.workspace`, workspace `reference`:

```bash
cd examples/reference-agent

# record the baseline release
python run_reference.py --release reference-agent-v1

# edit the scenario / agent_mock fixtures (or point at a changed agent),
# regenerate, and record the candidate release
python run_reference.py --generate --release reference-agent-rc2

# diff them (same workspace + registry both runs recorded into)
hotato release compare reference-agent-v1 reference-agent-rc2 \
    --workspace reference --registry ./.workspace
```

Two runs over identical fixtures record byte-identical results, so the diff
shows all-zero deltas and no status changes. Edit the fixtures between the
two runs to see movement.

The text output follows this shape (counts here are illustrative placeholders):

```
hotato release compare: baseline reference-agent-v1 -> candidate reference-agent-rc2  (workspace reference)
baseline runs=375 conv=375 eval=...  candidate runs=375 conv=375 eval=...
per-dimension counts (baseline -> candidate, delta; never blended):
  outcome       ...P/...F/...I  ->  ...P/...F/...I   (dP +.., dF -.., dI +..)
  policy        ...P/...F/...I  ->  ...P/...F/...I   (dP +.., dF -.., dI +..)
  conversation  ...P/...F/...I  ->  ...P/...F/...I   (dP +.., dF -.., dI +..)
  speech        ...P/...F/...I  ->  ...P/...F/...I   (dP +.., dF -.., dI +..)
  reliability   ...P/...F/...I  ->  ...P/...F/...I   (dP +.., dF -.., dI +..)
new failures (PASS on baseline -> FAIL on candidate): N
  - <scenario> / <dimension>
fixed since (FAIL on baseline -> PASS on candidate): N
  + <scenario> / <dimension>
all per-scenario status changes:
  ~ <scenario> / <dimension>: PASS -> FAIL
```

## Exit codes

- **`0`** -- the two releases were compared (per-dimension deltas and
  new-failures / fixed-since printed; a side with no runs is reported as an
  empty state, exit stays `0`).
- **`2`** -- a usage error or an unreadable registry `--registry`.

## See also

- [SUITE-RUN.md](SUITE-RUN.md) -- records the releases this command diffs.
- [CONVERSATION-TEST.md](CONVERSATION-TEST.md) -- the per-dimension scorecard model.
- [WORKSPACE.md](WORKSPACE.md) -- browse the releases and their runs with `hotato serve`.
