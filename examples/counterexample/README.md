# Counterexample compiler example

This example reduces a deterministic scripted outcome failure. The caller asks
for a refund and the mock `issue_refund` tool reports acceptance, while the
mock post-call state still says `refund_status: pending`. The selected
`state` assertion requires `posted`.

The source deliberately includes unrelated turns, a lookup tool, another order,
an unrelated state resource, environment metadata, and a variation matrix.
The compiler can delete a unit only when the same structured state failure
survives.

Run every supported operation used in the walkthrough:

```bash
sh examples/counterexample/run.sh
```

The script creates a new temporary working directory and leaves these outputs
for inspection:

```text
refund-not-posted.hotato-repro/  # private, runnable, content-bearing
refund-not-posted.share-safe/    # non-runnable metadata projection
```

The private capsule is suitable for `verify`, `reproduce`, and `predicate`.
The share-safe projection cannot run the fixture.

Read [`docs/COUNTEREXAMPLES.md`](../../docs/COUNTEREXAMPLES.md) before using a
capsule in CI. In particular, `reproduce` exits `0` when the failure remains;
`predicate` maps that outcome to exit `1` for evaluator bisect and shell gates.
