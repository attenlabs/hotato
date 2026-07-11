# Cards: a shareable image from any hotato result

`hotato card` turns a machine result into a self-contained SVG you can drop into
a pull request, an issue, or a slide. One command, offline, and honest by
construction: the card names the measured timing moment and never a verdict about
intent, and it carries no accuracy number anywhere.

```bash
hotato card INPUT[#REF] --out card.svg
```

Everything runs locally. The SVG is a pure function of the input JSON (no
timestamp, no version, no randomness), so the same input renders the same bytes
forever, and it references no font, image, stylesheet, script, or link: every
color is inline. Drop it anywhere with no CDN and no asset host.

## The four cards, auto-detected

The input's kind decides the card; you do not pick.

| Input | Card |
|---|---|
| a `sweep`/`analyze` candidate ref, `FILE#N`, that is a talk-over moment | **talk-over candidate** |
| a `sweep`/`analyze` candidate ref, `FILE#N`, that is a false-stop moment | **false-stop candidate** |
| a fix plan whose `decision` is `do_not_tune_single_threshold` | **threshold funnel** (the hero) |
| a supported `hotato verify` before/after rollup that improved | **paired comparison** |

`#N` is the same 1-based rank the sweep report and dashboard show, and it is the
same ref `hotato fixture promote` takes, so a card and a fixture speak of the
exact same moment.

### A. talk-over candidate

An `overlap_while_agent_talking` (or `agent_start_during_caller`) moment: the
agent kept the floor while the caller was speaking. The card leads with the
measured overlap in seconds and closes with "Hotato reports timing candidates,
not intent."

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato card hotato-sweep.json#3 --out talk-over.svg
```

### B. false-stop candidate

An `agent_stop_no_caller` moment: the agent went quiet with no caller nearby to
explain the drop. The card leads with the measured trailing silence.

```bash
hotato card hotato-sweep.json#1 --out false-stop.svg
```

### C. threshold funnel (the hero)

The plan the both-axes case produces: the battery missed a real interruption
**and** false-stopped on a backchannel, so no single sensitivity dial can satisfy
both axes at once. The card states that Hotato refused threshold tuning and names
the fix class (`engagement-control`). This is the card the project leads with.

```bash
hotato demo --format json > demo.json
hotato plan demo.json --out fix-plan.json
hotato card fix-plan.json --out no-single-threshold.svg
```

Only a `do_not_tune_single_threshold` plan renders this card; any other plan is
a clean exit-2 usage error (it is not one of the four kinds).

### D. paired comparison

A supported `hotato verify` before/after rollup where at least one previously-failing
fixture now passes and no hold/backchannel fixture regressed. This is paired
evidence, not a claim about the current agent standing alone -- the card
reads "PAIRED FRESH-RECAPTURE IMPROVED" only when the recapture is runner-
attested and "PAIRED (OPERATOR-ASSERTED)" otherwise, never "verified" or "fix verified", and
closes with "Hotato reports coincidence, not causation." A verify result that
does not support that claim (too few previously-failing fixtures, nothing now
passing, or a regressed hold fixture) is refused with exit 2 rather than
stamped as an improvement.

```bash
hotato card verify.json --out comparison.svg
```

## Redaction: safe to share by default

A card is a public image, so identifiers are hidden by default. A call id, a
filesystem path (only a basename is ever a candidate for display), and a vendor
recording name are omitted. A pulled recording named `STACK__ID.wav` carries the
call id inside its name; that name is only ever shown under
`--include-identifiers`.

```bash
# shows the source recording's basename on a candidate card
hotato card hotato-sweep.json#1 --out card.svg --include-identifiers
```

## Output and exit codes

Without `--out`, the SVG is written to stdout, so you can pipe it. With `--out`
it is written there atomically.

- **0**: the SVG card was rendered (to `--out`, or to stdout).
- **2**: usage error, unreadable input, a bad candidate ref, or an input that is
  not a fix plan / verify result / sweep candidate.

## Regenerating the committed cards

Three commit-ready examples live under `docs/assets/cards/`
(`no-single-threshold-card.svg`, `talk-over-card.svg`, `false-stop-card.svg`),
rendered from the two bundled demo calls. Regenerate them with:

```bash
PYTHONPATH=src python3 scripts/render_card_assets.py
```
