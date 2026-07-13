# D1.2 (P1A+P1B) gate report — Failure Record v1

## Commits
- P1A 7d87226 feat(record): add versioned five-lane failure record
- P1B 5fca4d1 feat(record): render the failure record to JSON, Markdown, HTML, SVG

## Deliverables
- src/hotato/schema/failure-record.v1.json (kind hotato.failure-record.v1)
- src/hotato/failure_record.py (projection + validate_record oracle; no evaluation)
- src/hotato/failure_render.py (JSON/MD/inert-HTML/1200x630-SVG, byte-deterministic)
- hotato record render SOURCE[#TEST_ID] --out DIR (cli.py; exit 0 / 2)
- tests: test_failure_record.py (35), test_failure_render.py (20), committed goldens,
  the reference kit's golden record+evidence (oracle parity durable in CI)

## Gate evidence (delta D1 + pack P1)
- Content-addressed: record_id = sha256(canonical identity), identity excludes the
  id and any wall-clock; byte-identical to the reference-kit oracle.
- Five lanes each on their own; NO aggregate score (mutation refused before
  closed-key check so a smuggled overall_score is caught).
- Outcome authority wall: transcript-only outcome refused at projection AND
  validation; outcome PASS/FAIL must cite tool/state/trace evidence.
- Safe projection: raw audio, transcript bodies, tool/state payloads, secrets,
  env values, absolute paths excluded by default; sentinel-secret test.
- Renderers: all four carry the same record_id; HTML/SVG inert (no script, no
  remote asset, hostile strings escaped); double-render byte-identical; offline
  (socket monkeypatched).
- Missing/malformed/unsupported never renders PASS; all-pass source refused with
  "source contains no failure".
- Reference kit standalone: generate.py --check + verify.py (13) + unittest (8) OK.
- Build: wheel ships the schema (schema/*.json package-data) + both modules;
  installed-wheel `record render` e2e OK.

## Interface decisions for downstream (Action / view / Atlas)
- pass_caret_k (kit+repo), not pass_power_k (pack). Wilson from repo ci block.
- gate.status = ERROR>FAIL>INCONCLUSIVE>PASS over deterministic assertions;
  gate.exit_code separate; gate.policy = "+".join(success.required).
- provenance.related carries before/after (schema-optional).
- reproduction argv = deterministic re-projection cmd, source pinned by digest.
