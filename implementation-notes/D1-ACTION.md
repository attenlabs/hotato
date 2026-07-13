# D1.3 (P4) gate report — consumer GitHub Action

## Commit
- 43766b5 feat(action): consumer-safe root GitHub Action

## Deliverables
- action.yml (composite; setup-python pinned by SHA + ci/action/gate.py)
- ci/action/{gate.py,summary.py} (stdlib-only, inputs via env not shell text)
- tests/fixtures/action-consumer/ (unrelated consumer repo; recorded CLI outputs
  for pass/mixed-fail/advisory-unavailable/inconclusive/absent-lanes/contract/
  malformed; a "with spaces" case)
- tests/test_action_consumer.py (24); docs/CI.md consumer section;
  .github/workflows/tests.yml action-smoke job; MANIFEST.in include action.yml

## Gate evidence
- Works from an unrelated fixture repo (uses: ./ smoke on this repo's CI).
- Five-lane job summary on pass AND failure, read from machine JSON; absent
  lanes NOT_RUN, missing evidence INCONCLUSIVE never PASS.
- Exits with hotato's gate status; a green exit with unreadable result raised to
  2; advisory lane never flips exit unless gate-advisory opted in.
- Third-party actions pinned by full SHA (supply-chain test); permissions
  contents:read; upload explicit; default gate installs no model/ASR/Node/judge.
- record render guarded (works before the Failure Record CLI; now present).

## Maintainer follow-up (external, cannot close from inside the repo)
- Create an external repo pinning uses: attenlabs/hotato@<SHA> and confirm the
  summary/outputs/exit on a real runner.
- Resolve the docs pin to the first shipping tag's SHA at release.
- Flip a consumer example to render-records: true now that the CLI exists.
- State Linux-only until a macOS Action matrix run passes.
