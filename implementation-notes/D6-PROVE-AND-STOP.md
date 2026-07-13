# D6 gate report — cold-path prove-and-stop

## Deliverables
- `scripts/verify_delta.py`: the machine-verifiable prove-and-stop gate. Checks,
  and exits non-zero unless ALL are green:
  1. freeze — the delta range (bf502a8..HEAD) touched only additive surfaces; no
     work started on any D0 frozen/reject surface.
  2. D2+D3 tests (interaction_label + capability_routing).
  3. adjacent offline security + consumer suites (fix_round3_security,
     fleet_security, release_supply_chain, action_consumer).
  4. D5 atlas tests (build_atlas present + test_atlas green, incl. build-twice
     determinism).
  5. D4 staged bundle present + its own tests green (saa-sdk not local; staged).
  6. cold-path evidence recorded, share-safe, >=1 cold battery exit 0.
- `scripts/cold_path_proof.py`: records share-safe cold evidence (command, exit,
  wall-time, artifact names + digests; no audio/transcript/identifiers/absolute
  paths) from a clean install of the released package in a fresh HOME + empty
  project dir, no key.
- `implementation-notes/evidence/cold-path-evidence.json`: the recorded proof —
  the credentialless `hotato start --demo` first-run completes cold with zero
  intervention and produces its full artifact set.
- `implementation-notes/evidence/human-battery-template.json`: the two human
  cold batteries (5 unfamiliar engineers; 5 hosted SAA starts) as an
  un-fabricated template with empty result slots and stable reason codes.

## The honesty invariant (why the human counts are empty)
06_MEASUREMENT's two cold batteries require real people and hosted credentials
that cannot be produced on the build machine. Fabricating "4/5 finished in
<5 min" would violate the no-fabricated-numbers rule AND defeat the purpose of
an honesty gate. So the verifier asserts ONLY machine-verifiable evidence; the
human batteries stay an explicit template until real cold users run them. A
blocker is a result, not permission to fabricate.

## Freeze / prove-and-stop
The D0 freeze and reject surfaces (Fleet/canary/hosted-accounts/second-judge/
etc.) were verified untouched by the delta. On a full green the verifier prints
"STOP — scope frozen, add no breadth." The delta program adds no feature breadth
beyond D2 labels, D3 routing, D5 atlas, and D6's own verifier.
