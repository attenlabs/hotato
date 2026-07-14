# Hotato Guardian / Fleet

Guardian/Fleet runs Hotato's capture → scan → trust → label → contract →
trial primitives as one continuous workflow inside a private, self-hosted
control plane: a hardened evidence kernel with a fleet view on top. Every
deploy to production stays a human call.

## The evidence kernel

### An evidence vector, scored dimension by dimension
Every artifact carries a machine-readable **evidence vector**
(`hotato/evidence.py`, schema `evidence_vector.v1.json`): each dimension
scored on its own, the public tier set to the *weakest* tier any required
dimension allows -- a minimum over an inspectable lattice. Tiers, ascending:

| tier | name | meaning |
|---|---|---|
| 0 | none | no usable evidence for a positive claim |
| 1 | asserted | envelope-only; not recomputed from audio |
| 2 | measured | recomputed from audio, one recording, input clean |
| 3 | paired | manifest-bound before/after, both sides recomputed under one pinned policy |
| 4 | attested | paired + signed policy/contract + runner-attested capture |

Dimensions: `score_integrity`, `audio_identity`, `policy_integrity`,
`fixture_set_integrity`, `input_health`, `channel_mapping`,
`label_authority`, `pairing_integrity`, `capture_origin`. One weak dimension
pulls the whole tier down; no renderer may raise it.

### Trial manifest (`hotato/manifest.py`, `trial_manifest.v1.json`)
An immutable pin created *before* an experiment from the battery: the scorer
config hash, one policy (applied to both sides), the complete fixture
universe, and each fixture's expectation, onset, and scripted-stimulus
identity. Before and after must reference the same `manifest_hash`; a
changed policy, scorer, label, onset, or fixture set refuses the comparison.

### Recompute-from-audio (`hotato/recompute.py`)
The proof gate re-derives every verdict from the on-disk audio under the
manifest, checks each stored `verdict.passed` against the recomputed
result, and refuses:

- **verdict tampering** -- a stored verdict that disagrees with the recomputed one;
- **same-audio re-encode** -- before and after decode to the same PCM;
- **dropped fixtures** -- either side omits a pinned fixture;
- **unrelated audio** -- the after-side caller stimulus does not match the pinned one and no capture receipt binds it.

A green *paired* proof additionally requires evidence tier >= paired.

### Capture receipt (`hotato/receipt.py`, `capture_receipt.v1.json`)
A fresh-recapture claim needs more than distinct PCM. A capture runner emits
a receipt binding the recording to a trial, agent, call id, timestamps, and
decoded PCM; an HMAC signature makes it *machine-verified*. A manual
distinct WAV with no receipt is *operator-asserted* -- the receipt is what
promotes it to machine-verified fresh recapture.

### Contract authenticity (`hotato/attest.py`)
Contracts embed a canonical digest over schema + label + policy + audio +
scorer version. `contract verify`/`unpack` recompute it: a bundle edited
after creation (a loosened policy re-pack, say) reports **tampered** and
fails; a matching but unsigned bundle is *"unsigned, internally consistent
evidence"*; a valid HMAC signature promotes it to *authenticated*.

### Boundary sensitivity
Every event exposes `onset_frame_index`, `onset_effective_sec`,
`decision_margin_sec`, `decision_margin_hops`, and `boundary_sensitive`. A
result within one hop of flipping is flagged, so it never reads with the
confidence of a result a hundred milliseconds inside the limit.

### Trust headline
Any verdict-changing warning (low signal, possible channel swap,
VAD-relevant leakage) sets the headline to `scan with caution`.
`input_health` is an explicit three-state field (`clean` / `caution` /
`not_scorable`). Leakage is judged both against a fixed -40 dB bar and
dynamically against the receiving channel's own VAD gate.

## Fleet control plane (local mode)

Local mode is zero-dependency: SQLite metadata plus a content-addressed
artifact directory, both stdlib.

- **Registry** (`hotato/fleet/registry.py`) -- workspace-scoped rows for
  agents, deployments, calls, recordings, candidates, labels, contracts,
  trials, decisions. Agents scale per workspace with no product-level cap;
  every row carries `workspace_id`, keeping paths and call ids scoped to
  their own workspace.
- **Artifact store** (`hotato/fleet/store.py`) -- content-addressed blobs
  with dedup and lineage; audio is stored separately from any UI (privacy
  reversal).
- **Job queue** (`hotato/fleet/jobs.py`) -- leased jobs with deterministic
  idempotency keys, heartbeats, retries, and dead-lettering, so duplicate
  webhooks and worker crashes converge on one logical result.
- **Guardian API** (`hotato/fleet/api.py`) -- ingest → discover (trust
  preflight + scan) → human review + label → manifest-bound before/after
  experiment that **recommends a change for you to apply**, and refuses
  forged / same-audio / incomplete trials.

### CLI

```
hotato fleet init -w acme
hotato fleet agent add -w acme --agent-id support-bot --stack vapi --assistant-id asst_123
hotato fleet ingest -w acme --agent support-bot call.wav
hotato fleet discover -w acme --agent support-bot call.wav
hotato fleet review -w acme
hotato fleet label <candidate-id> --decision yield --reviewer you
hotato fleet experiment run -w acme --agent support-bot --trial-id t1 \
    --battery before/run.json --before before/ --after after/
hotato fleet status -w acme
```

Live clone/recapture/canary run against a connected stack with credentials
and a tested rollback; every release recommends the change and keeps
routing production traffic a manual, human-gated step.

## Synthetic perturbations (`hotato/synth.py`)
Deterministic acoustic transforms of real fixtures -- resample, gain, noise,
leakage, channel invert, silence, onset offset, clip -- each carrying its
parent hash, recipe, seed, and an explicit synthetic designation. Synthetic
and real stay on separate axes: only a real recapture raises the confidence
tier.

## What fleet mode ships today
Evidence stays per-dimension, each one scored on its own lane, never folded
into a single fleet-wide score. Every contract needs human approval before
it exists. Live provider adapters (Vapi/Retell clone→apply→recapture) and
canary routing are gated on connected credentials and a tested rollback --
production deploys stay a manual, credentialed step every time.
