# The hotato contract wire format, as an open spec

This directory is the shipped contract wire format, extracted so anyone can
implement a reader or writer without importing hotato:

- [`contract.schema.json`](contract.schema.json): the JSON Schema (draft-07)
  of `contract.json`, the document at the heart of every `<id>.hotato`
  bundle. It is a byte-identical copy of the schema of record the package
  ships (`src/hotato/schema/contract.v1.json`), kept in lockstep by
  `tests/test_bench_cli.py`.
- [`CANONICALIZATION.md`](CANONICALIZATION.md): the exact canonical-bytes
  and content-addressing rules the implementation uses, with the file and
  function that implements each one.

## What the format is

A contract turns one recorded call moment into a portable, self-contained,
re-checkable claim: the audio, a human label for what the agent should have
done (`yield` or `hold`), the measured timing, an input-health report, a CI
pass/fail policy, and the exact commands to replay it. `hotato contract
verify` re-measures the same recording later and reports pass or fail with a
stable exit code, so the claim is checked by re-execution, not by trust. The
full bundle layout is documented in `docs/CONTRACTS.md`.

A contract records timing behavior against an explicit human label. The
label source is frozen to `"human"` in the schema itself, so a
machine-inferred label can never wear the format.

## Stability promise

- `schema: "hotato.contract.v1"` is additive-only: new keys may appear
  without a version bump, and a consumer must ignore unknown fields
  (`additionalProperties: true` throughout the schema is deliberate).
- A shipped field's meaning does not change. A breaking change is a new
  schema id (`hotato.contract.v2`), never a reinterpretation of v1.
- The canonicalization and content-addressing rules in
  [`CANONICALIZATION.md`](CANONICALIZATION.md) are part of the format:
  digests computed by one implementation must verify in another, so the
  byte rules are frozen with the same additive-only discipline.
- The schema of record lives in the package
  (`src/hotato/schema/contract.v1.json`); this copy tracks it byte for
  byte, and a divergence is a test failure in this repository.
