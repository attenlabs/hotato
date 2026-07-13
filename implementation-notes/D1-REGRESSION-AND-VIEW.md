# D1.4 (P5) + D1.5 gate report

## D1.4 regression prepare
- src/hotato/regression.py + `hotato regression prepare` (cli.py). Turns one
  confirmed failure into a sanitized deterministic bundle on disk; rights +
  redaction from versioned metadata files (no free-form consent on the CLI);
  never uploads/commits/PRs/changes an agent. Private vs public profiles.
  Refusals: missing metadata, digest mismatch, unsafe path, malformed schema,
  redaction sentinel remains, ambiguous channels. Reuses fixture/contract/
  failure_record safe-projection. tests/test_regression_prepare.py.
## D1.5 record view
- serve /records list + /records/<id> detail (app.py/data.py/render.py), a
  fifth "Failure records" tab. Token-gated on every path (landing stays the
  only unauthenticated page); record_id regex + realpath containment (traversal
  + symlink escape refused); 2MB limit; validate_record oracle before display;
  inert HTML (no script/remote asset, escaped); five lanes separate, gate apart
  from advisory. Empty state when no records. ?format=json returns the canonical
  record. tests/test_serve.py (+7). The record file open routed through
  errors.open_regular (guard test green).
## Gate
- Union targeted 130 passed after the open_regular fix; serve 35 passed.
- D1 COMPLETE: semantic demo (P0), Failure Record + renderers + CLI (P1A/P1B),
  consumer Action (P4, action-smoke green in CI), regression projection (P5),
  read-only record view. Shipped as 1.4.0.
