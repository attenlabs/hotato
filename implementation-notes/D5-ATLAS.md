# D5 gate report — Voice Failure Atlas (static builder)

## Deliverables
- Typed sources (single source of truth), schema-validated:
  - `atlas/records/addressed-interruption-missed.json` (fd-01),
    `atlas/records/addressed-backchannel-yielded.json` (fd-02) — each embeds
    a real `hotato.failure-record.v1` produced by actually running
    `hotato contract create` -> `hotato contract verify` -> `hotato record
    render` against the bundled share-safe `examples/funnel-demo/` fixtures,
    plus a verbatim `cli_transcript` of every command (TRUST-GALLERY.md
    worked-example style).
  - `atlas/contracts/{engagement-control,utterance-addressee-gate,
    turn-intent-discriminator}-v1.json` — the three `required_capability`
    values from `hotato.capability_routing`, described neutrally.
  - `atlas/implementations/{turn-intent-discriminator,utterance-addressee-
    gate}-generic.json` — per-stack recipes; the router-earned one
    (`status: evidenced`) and the documented-only one.
- `src/hotato/schema/atlas-{record,contract,implementation}.v1.json`.
- `scripts/build_atlas.py`: stdlib-only, deterministic builder. Computes
  every capability verdict by CALLING `hotato.capability_routing.
  route_capability` over each record's `routing_fixture` + its
  `paired_with` siblings — a typed source never carries a pre-baked routing
  outcome. Renders the full page graph, `/failures/index.json` +
  `/implementations/index.json`, and `llms.txt` / `llms-full.txt` /
  `sitemap.xml` / `feed.xml` from one shared in-memory page-graph list.
- `tests/test_atlas.py` (26 tests): schema+digest validation, build-twice
  byte-identical, link + machine-index resolution, the hard publication
  gate (including a tampered-digest refusal and an unearned
  `status: evidenced` refusal), and the backchannel-exclusion + neutrality
  invariants.

## Honesty invariants enforced by CODE, not by trusting the source
- **Capability verdicts are computed, never stored.** A record's JSON never
  says "this routes to X" — the builder pairs `routing_fixture` events by
  `paired_with` and calls the real D3 router. The funnel-demo battery's own
  evidence (fd-01 addressed-miss + fd-02 addressed-backchannel-false-trigger)
  routes to `turn_intent_discriminator`, confirmed against the shipped
  router, not asserted.
- **`utterance_addressee_gate` (the SAA-eligible class) ships with zero
  indexed records.** The bundled fixtures contain no cleared paired fixture
  with an explicit non-addressed-speech label, so
  `/failures/patterns/side-speech-triggered-agent/` renders as an honest,
  noindex, zero-member stub (referenced by the contract page instead of a
  dead link), and `/implementations/utterance-addressee-gate/generic/` is
  `status: documented` with `verified_against: []`.
- **An `evidenced` implementation claim is independently re-checked.**
  `verify_implementation_evidence` re-derives the router verdict for every
  `verified_against` record and hard-fails the build if it does not match
  the claimed capability (`tests/test_atlas.py::
  test_an_implementation_evidenced_claim_must_be_earned` pins this with a
  deliberately bogus claim).
- **Neutrality + authenticity lint** runs over every typed source's
  title/summary/approach/integration_points AND over every rendered page:
  no SAA/vendor mention anywhere except the mandated footer-only
  attribution line, no authenticity-protest wording.
- **Content-digest integrity.** Every typed source's `content_digest` is
  recomputed and compared on load; an edited-but-not-recomputed source
  hard-fails the build (`AtlasBuildError: content_digest mismatch`).

## Judgment calls / proposed paths (none pre-specified by the task)
- Output dir: `_atlas_site/` (gitignored), matching the task's first
  suggestion.
- `/implementations/{id}/{stack}/`: read `{id}` as the capability slug
  (`utterance-addressee-gate`, `turn-intent-discriminator`,
  `engagement-control`), so `/implementations/utterance-addressee-gate/`
  (no `{stack}`) is that capability's landing page and
  `/implementations/utterance-addressee-gate/generic/` is one stack's
  recipe — consistent with the one explicitly named landing route.
- `pattern_class` is not a fourth typed-source kind; it is a field on
  `atlas-record.v1` and pattern pages are DERIVED by grouping. A contract's
  `related_pattern_classes` can name a class with zero records, which the
  builder now renders as an honest stub page rather than a dead link
  (`all_referenced_pattern_classes`).
- `/benchmarks/{id}/`: renders for every record (one per `content_id`), not
  gated on `origin == "benchmark"` — "benchmark" here means the raw
  measurement view (matching `docs/BENCHMARK.md`'s usage of the word), not
  the narrower `origin` enum value. Both seeded records get one.
- `/integrations/{stack}/`: renders for all six stacks the CLI recognizes
  (`--stack {generic,vapi,twilio,livekit,pipecat,retell}`), not only the
  ones a record happens to reference, so the page graph has no stack-shaped
  gaps from the first build. A stack with 0 records renders an honest empty
  state and is noindex; only `generic` (both seeded records) is indexed.
- Reviewer identity: `hotato contract create` embeds
  `identity.reviewer` (from `--reviewer` / `HOTATO_REVIEWER` / `$USER`
  fallback) into its attestation digest. The transcripts were captured with
  `HOTATO_REVIEWER=hotato-examples` so no developer's personal OS username
  reaches a typed source or a public page.
- `atlas/`, `scripts/build_atlas.py`, and the three new
  `src/hotato/schema/atlas-*.v1.json` files are NOT added to
  `pyproject.toml`'s `package-data` — the schema files are already covered
  by the existing `schema/*.json` glob (they ship); `atlas/` and the build
  script are repo-level dev tooling, same footing as `examples/`/`docs/`,
  not part of the installed package.

## Not satisfied (and why)
- `/failures/patterns/refund-claimed-without-state-change/` and
  `/failures/patterns/disclosure-skipped-after-interruption/`
  (candidate slugs from `implementation-notes/STRATEGY-ADDENDUM.md`) are
  outcome/policy-dimension patterns; the bundled `examples/funnel-demo/`
  fixtures are turn-taking-only WAVs, so there is no real CLI-derived
  evidence to seed them from. Not created rather than fabricated.
- No copy-lint integration: `scripts/copy_lint.py` scans a fixed list of
  shipped-copy targets (README, llms.txt, docs/, report/card renderers) and
  was not extended to also scan `atlas/` or `_atlas_site/`. D5's own
  neutrality/authenticity lint (in `build_atlas.py`, tested in
  `test_atlas.py`) covers the same class of failure for atlas content, but
  the two lints are not unified.
- No CI job wiring: this delta ships the builder + tests; no
  `.github/workflows/*.yml` step was added to run `build_atlas.py` or gate
  on it. The task's scope was the builder and its tests, not CI wiring.

## Gate
- `python -m pytest tests/test_atlas.py -q`: **26 passed**.
- `python -m pytest tests/test_atlas.py tests/test_capability_routing.py tests/test_examples_funnel.py tests/test_copy_lint.py -q`:
  **48 passed** (adjacent tests unaffected).
- Full suite NOT run (operator instruction: new-atlas tests + directly
  adjacent ones only, ~7 min full suite skipped).
- `python3 scripts/build_atlas.py /tmp/.../out1` then again into `/tmp/.../out2`:
  `diff -rq` byte-identical.
- Committed locally in this worktree; NOT pushed, no PR opened (per
  instruction).
