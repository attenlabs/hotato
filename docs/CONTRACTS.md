# Failure contracts

A failure contract turns ONE call moment into a portable, private,
vendor-neutral bundle: the audio, frame-level timing evidence, an input-health
report, a shareable card, a CI pass/fail policy, and the exact commands to
replay and re-verify it. It is the CI object: `hotato contract verify` re-scores
a directory of contracts and exits non-zero when one regresses.

A contract's label always comes from a human call (`label_source` is frozen to
`"human"`); Hotato measures whether the recorded timing matched that label, and
`contract verify` re-measures the SAME recording later and reports pass/fail.

> **A contract bundle contains call audio (`audio/event.wav`).** Do not commit a
> raw customer contract to a public repository -- treat it like any other
> recording of a caller. Use sanitized fixtures (synthetic or consent-cleared)
> for anything public, and put customer contracts in a private repository or
> controlled artifact storage. `--include-identifiers` additionally writes a
> source basename and candidate ref into `contract.json` and the card; leave it
> off by default for the same reason. That redaction covers metadata only -- the
> full audio-handling rules are in
> [`SECURITY.md`](../SECURITY.md#audio-handling).

## Two lanes: what `verify` proves depends on which recording you feed it

| | `contract verify` on the frozen recording | `contract verify` on a fresh recapture |
| --- | --- | --- |
| **What it re-scores** | The SAME `audio/event.wav` the contract was created from | A new recording of the SAME stimulus against your CURRENT agent |
| **What a pass proves** | The evidence, the policy, and the scorer are still intact and still agree with the human label | The CURRENT agent's behavior on that stimulus still matches the label |
| **What a pass does NOT prove** | That the deployed agent's behavior has not changed since | Nothing extra -- this is the one that speaks to the live agent |
| **When it runs** | Every push, in the shipped `ci/github-action.yml` (`contract verify contracts/`) | Only when you recapture by hand or on a schedule; see [`docs/RECAPTURE.md`](RECAPTURE.md) |

A frozen-recording pass is necessary but not sufficient to know the agent bug
stayed fixed: it can only fail if someone edits the bundle's audio or policy,
because the recording never changes. Proving the CURRENT agent still yields
correctly requires re-running the same caller stimulus against it and
re-verifying the fresh capture, by hand ([`docs/RECAPTURE.md`](RECAPTURE.md)).
Run the frozen gate on every push to catch evidence/policy drift for free, and
recapture periodically (or after an agent change) to catch what only a fresh
capture can.

## The bundle

`hotato contract create` writes one self-contained directory, `<id>.hotato/`:

```
refund-cutoff-001.hotato/
  contract.json                      # the contract itself (schema hotato.contract.v1)
  audio/event.wav                    # the (clipped) two-channel recording, or the mono file
  evidence/
    frames.jsonl                     # per-frame timing evidence behind every measurement
    timeline.html                    # the to-scale caller/agent timeline, self-contained
    trust.json                       # the input-health (trust doctor) report
    card.svg                         # a shareable 1200x630 SVG card (redacted by default)
  traces/                            # empty until `hotato trace attach` (see docs/TRACE.md)
  source/
    call_metadata.json               # redacted-by-default: stack, category, expect
    stack_config_snapshot.json       # placeholder until populated by hand
  policy/verify.yaml                 # the SAME subset `hotato verify --policy` reads
  reports/
    initial.html                     # the full scored report at creation time
    after.html                       # placeholder until a fix is re-captured and verified
  provenance.json                    # who/when/how this contract was created
  ci/
    github-action.yml                # a weekly + on-push CI scaffold
    junit.xml                        # this ONE contract's JUnit result at creation time
```

Every path above is also recorded, machine-readable, in
`contract.json["bundle"]["paths"]`.

## Create

From a candidate a sweep or scan already surfaced:

```bash
hotato sweep --demo --format json > hotato-sweep.json
hotato contract create --from-candidate hotato-sweep.json#1 \
    --expect yield --id refund-cutoff-001 --out contracts
```

From a raw two-channel recording you already have:

```bash
hotato contract create --stereo bad-call.wav --onset 42.18 \
    --expect yield --id refund-cutoff-001 --out contracts \
    --max-talk-over 0.6 --max-time-to-yield 1.0
```

Both forms wrap the SAME round-trip guarantee `hotato fixture create` gives: the
moment is scored immediately, and a not-scorable input (the agent silent at the
onset, an unreadable file, a bad channel map) is refused with a clear reason and
exit code 2 -- no bundle is written. A single-channel (mono) recording passed as
`--stereo` is rejected the same way `fixture create` rejects it: caller and
agent cannot be told apart on one channel.

`--caller FILE --agent FILE` (two mono WAVs) is a third input form, scored and
clipped identically.

### Redaction

By default the bundle and card hide a candidate ref and a source recording's
basename. Pass `--include-identifiers` to show them (in
`source/call_metadata.json`, `contract.json`, and `evidence/card.svg`).

### The opt-in diarized-mono path

A single-channel recording can still become a contract through the SAME
quality-gated diarizer front-end `hotato run --mono --diarize` uses:

```bash
hotato contract create --mono call.wav --diarize \
    --expect yield --id refund-cutoff-002 --out contracts
```

This keeps an indicative-only verdict in its tier: a `low`-confidence separation
tier carries `measurement.indicative_only: true` all the way into
`contract.json` and every renderer, and a `refuse` tier (not two clean parties,
extreme overlap, unstable segmentation, voices too similar) is refused exactly
like a plain mono file, with the specific reason. Frame-level evidence
(`evidence/frames.jsonl`, the to-scale timeline) isn't produced for this path in
this release; `evidence/timeline.html` states that plainly and points at
`evidence/trust.json`'s separation confidence tier.

## Verify

```bash
hotato contract verify contracts/
hotato contract verify contracts/ --format json --junit contracts-junit.xml
hotato contract verify contracts/refund-cutoff-001.hotato --html verify.html
```

`DIR` is a contracts directory (every `*.hotato` subdirectory that carries a
`contract.json`) or one bundle directly. For each contract, `verify` re-scores
the SAME bundled audio against the SAME policy recorded in its own
`contract.json` -- this is what changes after an engine upgrade, a threshold
change, or a re-captured recording swapped into `audio/event.wav` -- and reports
pass/fail per contract and overall.

Exit codes are the CI contract: `0` every contract passes, `1` at least one
regressed (or is no longer scorable) OR at least one embedded assertion
deterministically FAILed (reported separately below, but it still contributes to
this nonzero exit), `2` a usage error, an empty directory, or a corrupt
`contract.json`. `--junit` writes one `<testcase>` per contract; the shipped
`ci/github-action.yml` scaffold runs this on push, on PR, and weekly, and
publishes the JUnit file as an artifact.

Every text and HTML render of `verify` also prints, verbatim: *"This result
re-measures stored evidence. It does not test the current agent."* -- a green CI
run here means the evidence, policy, and scorer are still intact; re-checking
today's deployed agent needs the fresh-recapture lane above. See
[`docs/RECAPTURE.md`](RECAPTURE.md#claim-language-what-each-kind-of-evidence-lets-you-accurately-say)
for exactly what each kind of evidence lets you claim.

### Embedded assertions (optional)

A contract can carry its own `assertions` block (schema `hotato.contract.v1` --
see `schema/contract.v1.json`), the same `{version, assertions}` document
`hotato assert` reads from an `assertions.yaml` file. When present, `contract
verify` evaluates it through the SAME `assert.v1` engine and reports the result
as a per-contract `assertions` field, kept separate from the timing pass/fail in
`summary`/`passed`. Pass `--transcript FILE` (a plain JSON array of `{role,
text, start, end}` turns, or hotato's own `{"segments": [...]}` shape) to supply
transcript context for `phrase`/`pii`/`policy` assertions; `tool_call`
assertions read the bundle's own attached trace (`hotato trace attach`) if one
exists. Missing context (no `--transcript`, no attached trace) reports
`INCONCLUSIVE`. A deterministic assertion FAIL contributes to the batch's
nonzero exit code exactly like a timing regression; the batch result also
carries a separate `assertions_failed` count. See
[`schema/assert.v1.json`](../src/hotato/schema/assert.v1.json) for the result
shape and `hotato.assert_` for the five deterministic kinds.

## Inspect

```bash
hotato contract inspect contracts/refund-cutoff-001.hotato
hotato contract inspect contracts/refund-cutoff-001.hotato/contract.json --format json
```

## Pack / unpack

A bundle directory travels as one file:

```bash
hotato contract pack contracts/refund-cutoff-001.hotato
# -> contracts/refund-cutoff-001.hotato.pack (deterministic; a MANIFEST.sha256.json
#    of every member travels inside it)

hotato contract unpack contracts/refund-cutoff-001.hotato.pack --out contracts/refund-cutoff-001.hotato
# every member is verified against the packed sha256 manifest; any mismatch
# (a corrupt or tampered archive) is refused (exit 2) and nothing partial is
# left behind
```

Packing the SAME bundle directory twice produces byte-identical archives (sorted
member order, fixed timestamps, and every other value written into each member's
ZipInfo, including its `create_system` byte): a contract's pack is a pure
function of its bundle contents, deterministic for a fixed hotato version.
Byte-identical re-runs are verified in CI on Linux x86_64, Python 3.10, 3.11,
and 3.12; the same suite now also runs on macOS and Windows in CI, not yet green
-- see [VALIDATION.md](VALIDATION.md) Job 1.

### Security: unpack treats an archive as hostile input

A `.hotato` archive is meant to be sent between teams, so `contract unpack`
verifies everything about it before trusting a single byte. Before or during
extraction it refuses (exit 2) on any of the following, writing only inside a
scratch temp directory that's removed on any failure:

* path traversal (`..`), absolute paths, and Windows-style backslash /
  drive-letter paths (`C:\...`) in a member name;
* symlink members and encrypted members;
* duplicate member names;
* any member the archive carries that its own `MANIFEST.sha256.json` does not
  declare;
* more members than a legitimate bundle could plausibly need;
* a declared or measured decompressed size past the cap (default 512 MiB, set
  `HOTATO_CONTRACT_MAX_UNPACK_BYTES` or pass `--max-bytes` to raise it for a
  trusted archive) -- checked against the bytes measured live during extraction,
  beyond what the archive's own (untrusted) size metadata claims;
* a single member whose compression ratio is far beyond anything a legitimate
  bundle member produces (a zip-bomb signal), even under the total-bytes cap.

The sha256-per-member check (above) still runs on top of all of this: a member
can pass every hardening check and still be refused for not matching the packed
manifest.

## CI

The shipped `ci/github-action.yml` is the minimal wiring:

```bash
uvx hotato contract verify contracts/ --junit contracts-junit.xml --format json > contracts-verify.json
```

Gate a pull request or a scheduled job on the process exit code; do not parse
stdout to decide pass or fail.

## What a contract proves, precisely

Hotato proves timing behavior against this explicit contract -- not
authorization, identity, compliance, or policy safety. `verify` reports
coincidence, not causation: a passing re-verify after a config change coincides
with the change, distinct from a controlled experiment. See
`docs/VALIDATION.md` and `docs/THREAT-MODEL.md`.

## Read more

- Proving the CURRENT agent, not just the frozen recording:
  [`docs/RECAPTURE.md`](RECAPTURE.md)
- The underlying regression-fixture primitive `contract create` wraps:
  [`docs/BAD-CALL-TO-CI.md`](BAD-CALL-TO-CI.md)
- Battery-scale before/after proof: [`docs/FIX-LOOP.md`](FIX-LOOP.md)
- Input-health (trust doctor): [`docs/TRUST.md`](TRUST.md) ·
  [`docs/TRUST-MATRIX.md`](TRUST-MATRIX.md)
- Diarized-mono scoring: [`docs/DIARIZE.md`](DIARIZE.md)
- Shareable cards: [`docs/CARDS.md`](CARDS.md)
- Attaching a voice trace (observability bridge): [`docs/TRACE.md`](TRACE.md) ·
  [`docs/OTEL.md`](OTEL.md)
- Audio-handling rules (what raw audio may contain, redaction limits,
  distribution): [`SECURITY.md`](../SECURITY.md#audio-handling)
