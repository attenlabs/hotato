# hotato bench: specification v0.1

hotato bench is a versioned freeze of the scenario batteries this repository
already ships, a scoring protocol over them, and a verify command that
re-executes a result and compares hashes. It is a benchmark you can hold in
your hand: a fixed set of labelled recordings, a fixed scorer, and a result
file whose numbers anyone can recompute from the same bytes. It is not a
leaderboard.

Run it:

```bash
hotato bench run --out bench-bundled.json          # the packaged battery
hotato bench run --suite gold --out bench-gold.json
hotato bench verify bench-gold.json                # re-execute + hash-compare
```

## Neutral harness

hotato is the measuring instrument, never a party to the comparison. This
repository publishes exactly two kinds of rows:

- rows measured on hotato's own reference renders (the frozen batteries
  below, scored as shipped);
- rows measured on fully open-source baseline agents, named and pinned by
  commit.

Rows for any vendor stack are adopter-published: the adopter who operates
that stack runs the bench, publishes the result in their own repository
under their own provenance block, and owns the claim. This repository never
authors a comparative row for a named vendor, and a pull request adding one
is declined.

## Scope: the frozen set

Bench v0.1 freezes the four tiered batteries under `corpus/suites/`
(112 scenarios; inventory in `corpus/suites/manifest.json`) plus the
packaged 8-scenario battery that installs with the package:

| Battery | Tier | Scenarios | Conditions | Suite content hash |
| --- | --- | --- | --- | --- |
| `bundled` | (package) | 8 | the packaged barge-in battery; runs from any install | `sha256:a02a4550c65fc0c8c897c523dac14d92bfb70b58cef6c8b1b70c59e2b6af8f5f` |
| `silver` | silver | 40 | clean, 16 kHz, default noise floor | `sha256:9ce2d70e9499880ca8b692796c4c7329dba8c15e6db579e2aa8ae7e8816e6c97` |
| `silver-defects` | silver | 16 | clean conditions, deliberate defect renders | `sha256:db948076fe9f617cbf1d901ef552a56dbb4858a4b2b7cd0f46e4b9244e079713` |
| `gold` | gold | 40 | noise floors, 8 kHz telephony, gain extremes, echo, edge timings, endurance | `sha256:f143781dbd1d09d3ca1bb1776ee0e2b2cf3c995f73e134aebedeee06575a307b` |
| `gold-defects` | gold | 16 | hard-condition defect renders, including two labelled capture-defect cases | `sha256:d1fd0bebb98650d51d6672861b4b29c9cbf3c061c46ef5ecd0bf0712388c2ea2` |

Every scenario is synthetic and says so: deterministic shaped noise rendered
from the exact segment timings in its own JSON (seed = `sha256(scenario_id)`),
so the timings are the ground truth and a fresh render is byte-identical to
the committed battery (`python3 corpus/suites/build_suites.py --check` is the
proof, and CI runs it). The defect batteries fail by design: they are the
negative control that makes the passing batteries mean something
(`docs/SUITES.md`).

A suite content hash pins the exact files a run consumes: one line per file,
`<relative_name>\0<file_sha256_hex>\n` over the sorted scenario JSONs and
their `<id>.example.wav` recordings, hashed with sha256
(`hotato.bench.suite_content_hash`).

## Versioning

- **Bench version** (`bench_version`, semver, currently `0.1`): the frozen
  set and the protocol together. Any change to any pinned file, to the audio
  suffix, to the hashed body shape, or to the hashing rules is a new bench
  version. The table above is the v0.1 pin list; a row change and a version
  bump land in the same commit.
- **Result schema** (`schema_version`, currently `1`): the shape of the
  result document. Additive-only within a major; a consumer must ignore
  unknown fields.
- **Scorer identity**: every result embeds the vendored engine identity
  (`engine`) and the full default `ScoreConfig` snapshot (`config`), so a
  result names the exact scorer and thresholds that produced it.

## Scoring protocol

Bench rows measure talk over on the hangover smoothed activity tracks;
relative to raw energy labels the end bias bound is `hangover_sec` plus one
hop (see docs/BENCHMARK.md, Quantization). This convention is part of the
frozen v0.1 protocol; changing it is a new bench version and a new engine
identity.

One result per battery, three measurements, side by side and never blended:

- **Pass counts** (`pass_counts`): scenarios, passed, failed, and
  not-scorable counts from the standard envelope
  (`hotato.core.run_suite` under the default shipped config). Results group
  by the battery's tier; a tier is a grouping label, never a weight.
- **Measurement-error distributions** (`error_stats_ms`): per signal
  (caller onset, time to yield, response gap), the n / median / mean / max /
  min of `|measured - rendered|` in milliseconds, against each scenario's
  own `reference_render` timings (`hotato.benchmark.run_benchmark`). A
  signal with no rendered ground truth reports `n: 0` and nulls, never a
  fabricated reference.
- **Confusion cells** (`confusion`): the four `did_yield` cells
  (correct_yield, missed_yield, false_yield, correct_hold) plus their
  off-diagonal sum.

There is no blended score. No field anywhere in a bench result aggregates
across these measurements, and no accuracy percentage appears; collapsing
the error distribution and the confusion matrix into one figure hides
exactly the missed-yield / false-yield trade-off the bench exists to
surface. This is code-enforced: the repo-wide `overall_score` rejection
(`hotato.errors.reject_overall_score`) applies to every nested section of a
bench result, and `bench verify` refuses a result that smuggles one in.

## Reproducibility

Byte-for-byte means: two executions of `bench run` on the same frozen
battery under the same package version produce result bodies whose
canonical JSON bytes are identical, so their sha256 addresses are equal.
Canonical JSON here is the form the rest of the repository already uses
(`hotato.manifest.canonical_json`): sorted keys, compact separators, ASCII,
finite numbers only. The result carries no wall-clock field, so its address
is stable across re-runs.

The whole path is offline: the batteries are local files (or packaged
data), the scorer is the vendored stdlib-only engine, and no code path in
`bench run` or `bench verify` touches the network. A machine with the
package and the frozen batteries can produce and check every number in a
result with no account, no key, and no service.

## Submission model: adopter-published rows

An adopter publishes a row by committing the evidence, not by sending a
score:

1. run the bench in their own CI: `hotato bench run --suite <name> --out
   results/<name>.json`;
2. gate the row on re-execution in the same pipeline: `hotato bench verify
   results/<name>.json` (exit 0 required);
3. commit the result files and a provenance block to their own repository,
   and render whatever badge they choose from their own CI status.

The provenance block published next to the rows carries, at minimum:

| Field | Content |
| --- | --- |
| `bench_version` | the bench semver the rows were measured under |
| `suite` / `suite_content_hash` | each battery name and its pinned hash, from the result |
| `result_content_hash` | each result's canonical sha256 address |
| `engine` | the vendored scoring-engine identity from the result |
| `config` | the `ScoreConfig` snapshot from the result |
| `runner` | OS, Python version, and CI system the rows were produced on |
| `commit` | the adopter repository commit the rows were produced from |
| `published_by` | who stands behind the row |

A row is the adopter's claim, verified by the adopter's own re-execution.
This repository links to adopter rows as published; it does not restate,
rank, or merge them.

## Anti-gaming

- **Verification is re-execution.** `bench verify` re-runs the pinned
  battery through the same code path `bench run` uses and compares the two
  canonical result addresses. The verdict is a hash comparison of two
  executions on the verifier's machine; no judge, no model, no service
  scores anything.
- **Edits are caught twice.** A result edited in place fails its own
  embedded `content_hash` and is refused. A result whose body and hash were
  both rewritten passes the integrity check and is then caught by
  re-execution, because the recomputed body hashes differently.
- **The frozen set is pinned.** A verify against a battery whose content
  hash differs from the one the result pins is withheld, not guessed: the
  comparison only ever runs like against like.
- **The batteries are public, and the pin makes that safe to say.** A stack
  tuned to the published scenarios still has to produce its numbers through
  the pinned scorer at the pinned config, and its provenance block names
  the exact config that produced every number.
- **The held-out check is a manual maintainer spot check.** A maintainer
  re-renders the frozen batteries from the generator, re-executes a
  published row by hand on maintainer hardware, and hash-compares. There is
  no hosted scoring server to game: every verdict, including the spot
  check, is a local re-execution.
