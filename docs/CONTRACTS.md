# Failure contracts

A failure contract turns ONE real call moment into a portable, private,
vendor-neutral bundle: the audio, frame-level timing evidence, an
input-health report, a shareable card, a CI pass/fail policy, and the exact
commands to replay and re-verify it. It is the CI object: `hotato contract
verify` re-scores a directory of contracts and exits non-zero when one
regresses.

Hotato does not infer intent. A contract's label is always a human call
(`label_source` is frozen to `"human"`); Hotato measures whether the recorded
timing matched that label, and `contract verify` re-measures the SAME
recording later and reports pass/fail.

Hotato does not prove authorization, identity, compliance, or policy safety.
Hotato proves timing behavior against this explicit contract.

## The bundle

`hotato contract create` writes one self-contained directory,
`<id>.hotato/`:

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

Both forms wrap the SAME round-trip guarantee `hotato fixture create` gives:
the moment is scored immediately, and a not-scorable input (the agent silent
at the onset, an unreadable file, a bad channel map) is refused with the
honest reason and exit code 2 -- no bundle is written. A single-channel
(mono) recording passed as `--stereo` is rejected the same way `fixture
create` rejects it: caller and agent cannot be told apart on one channel.

`--caller FILE --agent FILE` (two mono WAVs) is a third input form, scored
and clipped identically.

### Redaction

By default the bundle and the card hide a candidate ref and a source
recording's basename. Pass `--include-identifiers` to show them (in
`source/call_metadata.json`, `contract.json`, and `evidence/card.svg`).

### The opt-in diarized-mono path

A single-channel recording can still become a contract through the SAME
quality-gated diarizer front-end `hotato run --mono --diarize` uses:

```bash
hotato contract create --mono call.wav --diarize \
    --expect yield --id refund-cutoff-002 --out contracts
```

This NEVER silently upgrades an indicative-only verdict: a `low`-confidence
separation tier carries `measurement.indicative_only: true` all the way into
`contract.json` and every renderer, and a `refuse` tier (not two clean
parties, extreme overlap, unstable segmentation, voices too similar) is
refused exactly like a plain mono file, with the specific reason. Frame-level
evidence (`evidence/frames.jsonl`, the to-scale timeline) is not produced for
this path in this release; `evidence/timeline.html` says so plainly instead
of fabricating one, and points at `evidence/trust.json`'s separation
confidence tier.

## Verify

```bash
hotato contract verify contracts/
hotato contract verify contracts/ --format json --junit contracts-junit.xml
hotato contract verify contracts/refund-cutoff-001.hotato --html verify.html
```

`DIR` is a contracts directory (every `*.hotato` subdirectory that carries a
`contract.json`) or one bundle directly. For each contract, `verify`
re-scores the SAME bundled audio against the SAME policy recorded in its own
`contract.json` -- this is what changes after an engine upgrade, a threshold
change, or a re-captured recording swapped into `audio/event.wav` -- and
reports pass/fail per contract and overall.

Exit codes are the CI contract: `0` every contract passes, `1` at least one
regressed (or is no longer scorable), `2` a usage error, an empty directory,
or a corrupt `contract.json`. `--junit` writes one `<testcase>` per contract
for a CI dashboard; the shipped `ci/github-action.yml` scaffold runs this on
push, on PR, and weekly, and publishes the JUnit file as an artifact.

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

Packing the SAME bundle directory twice produces byte-identical archives
(sorted member order, fixed timestamps): a contract's pack is a pure function
of its bundle contents.

## CI

The shipped `ci/github-action.yml` is the minimal wiring:

```bash
uvx hotato contract verify contracts/ --junit contracts-junit.xml --format json > contracts-verify.json
```

Gate a pull request or a scheduled job on the process exit code; do not parse
stdout to decide pass or fail.

## What a contract does not prove

Hotato does not prove authorization, identity, compliance, or policy safety.
Hotato proves timing behavior against this explicit contract. `verify`
reports coincidence, not causation: a passing re-verify after a config change
coincides with the change; it is not a controlled experiment. See
`docs/VALIDATION.md` and `docs/THREAT-MODEL.md`.

## Read more

- The underlying regression-fixture primitive `contract create` wraps:
  [`docs/BAD-CALL-TO-CI.md`](BAD-CALL-TO-CI.md)
- Battery-scale before/after proof: [`docs/FIX-LOOP.md`](FIX-LOOP.md)
- Input-health (trust doctor): [`docs/TRUST.md`](TRUST.md) ·
  [`docs/TRUST-MATRIX.md`](TRUST-MATRIX.md)
- Diarized-mono scoring: [`docs/DIARIZE.md`](DIARIZE.md)
- Shareable cards: [`docs/CARDS.md`](CARDS.md)
- Attaching a voice trace (observability bridge): [`docs/TRACE.md`](TRACE.md) ·
  [`docs/OTEL.md`](OTEL.md)
