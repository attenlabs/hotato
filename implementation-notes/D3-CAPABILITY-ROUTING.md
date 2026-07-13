# D3 gate report — capability routing

## Deliverables
- src/hotato/schema/capability-requirement.v1.json (kind
  hotato.capability-requirement.v1): provider-neutral routing verdict —
  capability id, evidence refs, acceptance tests, input-health causes checked
  and cleared, optional neutral contract URI. Names no product/vendor.
- src/hotato/capability_routing.py (stdlib-only, pure, deterministic): reads
  SUPPLIED interaction labels on a paired addressee-control battery and returns
  the narrowest capability the paired evidence supports, or None. Truth table:
  - addressed floor bid missed + non-addressed speech false trigger
        -> utterance_addressee_gate (paired_discrimination_failure)
  - addressed floor bid missed + addressed feedback false trigger
        -> turn_intent_discriminator (paired_discrimination_failure)
  - either event lacks a trusted addressee/intent label
        -> engagement_control (insufficient_labels), missing axes listed
  - echo / non-speech ambient / invalid channel map / unscorable input
        -> None (a config / input-health finding, never a capability)
  - a lone event with no opposite-risk pair
        -> None (no paired discrimination claim)
- tests/test_capability_routing.py + 7 tests/data/routing/*.json fixtures, one
  per branch above (addressed-interruption, non-addressed-speech,
  addressed-backchannel, self-echo, non-speech-ambient, unknown-addressee,
  unscorable-non-addressed).

## Reconciliation (worktree branched pre-D2)
The D3 worktree agent forked from HEAD-before-D2, so it also rebuilt
interaction_label.py / its schema / its test with helpers the committed D2
module lacked. Only the D3-additive files were kept; the duplicate label module
was DISCARDED. The committed D2 interaction_label.py instead gained, additively:
- coerce(mapping): adapt a BARE label mapping (absent axes -> unknown/null,
  non-speech blanks pinned, contradictions still fail through validate). This is
  distinct from of(carrier), which reads a label attached under
  carrier["interaction_label"] and returns all-unknown when absent. The router
  passes an event's "interaction" object directly, so it reads through coerce(),
  not of() — the one router call was patched _il.of(...) -> _il.coerce(...).
- is_trusted / addressee_known / intent_known over TRUSTED_AUTHORITIES
  (human, trusted-source, fixture) — the routing-eligibility predicates.

## Invariant honored
- NEVER infers addressee or turn intent, never reads audio. The router imports
  only interaction_label; it derives nothing from timing, energy, transcript, or
  a model verdict. One timing threshold fails in both directions, so a lone
  "the agent talked over me" report makes no capability claim.
- Provider-neutral: no implementation, product, or vendor named in the verdict
  or schema.
- Core stays zero runtime deps (pure Python; validator cross-check in tests).

## Gate
- Full suite 2912 passed / 14 skipped / 0 failed (green baseline pre-release).
- Released in v1.4.1 (with D2). Lockstep version + llms-full + trust-gallery pin
  regenerated; wheel and sdist ship both modules and both schemas at 1.4.1.

## For D4 (saa-sdk trace sink)
- D4 writes a local-branch trace sink in saa-sdk that emits these two artifacts
  (interaction-label.v1 records, capability-requirement.v1 verdicts) from a run;
  NO push. The capability id + evidence-ref shape here is the contract D4 fills.
