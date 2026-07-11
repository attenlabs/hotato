# Hotato Guardian / Fleet

Guardian/Fleet turns Hotato's capture → scan → trust → label → contract → trial
primitives into one continuous, honest workflow, wrapped in a private,
self-hosted control plane. It hardens the evidence kernel first, then layers a
fleet view on top. Nothing here deploys to production automatically.

## The evidence kernel (hardened)

### Evidence vector, not a confidence percentage
Every artifact carries a machine-readable **evidence vector** (`hotato/evidence.py`,
schema `evidence_vector.v1.json`). The public tier is the *weakest* tier any
required dimension allows — a minimum over an inspectable lattice, never a blended
"92% confidence". Tiers, ascending:

| tier | name | meaning |
|---|---|---|
| 0 | none | no usable evidence for a positive claim |
| 1 | asserted | envelope-only; not recomputed from audio |
| 2 | measured | recomputed from audio, one recording, input clean |
| 3 | paired | manifest-bound before/after, both sides recomputed under one pinned policy |
| 4 | attested | paired + signed policy/contract + runner-attested capture |

Dimensions include `score_integrity`, `audio_identity`, `policy_integrity`,
`fixture_set_integrity`, `input_health`, `channel_mapping`, `label_authority`,
`pairing_integrity`, `capture_origin`. One weak dimension pulls the whole tier
down; no renderer may raise it.

### Trial manifest (`hotato/manifest.py`, `trial_manifest.v1.json`)
An immutable pin created *before* an experiment from the battery: the scorer
config hash, one policy (applied to both sides), the complete fixture universe,
and each fixture's expectation, onset, and scripted-stimulus identity. Before and
after must reference the same `manifest_hash`; a changed policy, scorer, label,
onset, or fixture set refuses the comparison.

### Recompute-from-audio (`hotato/recompute.py`)
The proof gate never trusts a stored `verdict.passed`. It re-derives every
verdict from the on-disk audio under the manifest and refuses:

- **verdict tampering** — a stored verdict that disagrees with the recomputed one;
- **same-audio re-encode** — before and after decode to the same PCM;
- **dropped fixtures** — either side omits a pinned fixture;
- **unrelated audio** — the after-side caller stimulus does not match the pinned one and no capture receipt binds it.

A green *paired* proof additionally requires evidence tier ≥ paired.

### Capture receipt (`hotato/receipt.py`, `capture_receipt.v1.json`)
A fresh-recapture claim needs more than distinct PCM. A capture runner emits a
receipt binding the recording to a trial, agent, call id, timestamps, and decoded
PCM; an HMAC signature makes it *machine-verified*. A manual distinct WAV with no
receipt is *operator-asserted*, never machine-verified fresh recapture.

### Contract authenticity (`hotato/attest.py`)
Contracts embed a canonical digest over (schema + label + policy + audio + scorer
version). `contract verify`/`unpack` recompute it: a bundle edited after creation
(e.g. a loosened policy re-pack) reports **tampered** and fails; a matching but
unsigned bundle is *"unsigned, internally consistent evidence"*, never
*authenticated*; a valid HMAC signature is *authenticated*.

### Boundary sensitivity
Every event exposes `onset_frame_index`, `onset_effective_sec`,
`decision_margin_sec`, `decision_margin_hops`, and `boundary_sensitive`. A result
within one hop of flipping is flagged, so it no longer reads with the confidence
of one hundreds of milliseconds inside the limit.

### Honest trust headline
Any verdict-changing warning (low signal, possible channel swap, VAD-relevant
leakage) forces `scan with caution`, never `safe to scan`. `input_health` is an
explicit three-state field (`clean` / `caution` / `not_scorable`). Leakage is
judged both against the fixed −40 dB bar and dynamically against the receiving
channel's effective VAD gate.

## Fleet control plane (local mode)

Local mode is zero-dependency: SQLite metadata plus a content-addressed artifact
directory (both stdlib).

- **Registry** (`hotato/fleet/registry.py`): workspace-scoped rows for agents,
  deployments, calls, recordings, candidates, labels, contracts, trials,
  decisions. No product-level cap on registered agents; every row carries
  `workspace_id`, so no global path or call id reaches another workspace.
- **Artifact store** (`hotato/fleet/store.py`): content-addressed blobs with
  dedup and lineage; audio is stored separately from any UI (privacy reversal).
- **Job queue** (`hotato/fleet/jobs.py`): leased jobs with deterministic
  idempotency keys, heartbeats, retries, and dead-lettering, so duplicate
  webhooks and worker crashes converge on one logical result.
- **Guardian API** (`hotato/fleet/api.py`): ingest → discover (trust preflight +
  scan, never auto-label) → human review + label → manifest-bound before/after
  experiment that **recommends, never auto-deploys**, and refuses forged /
  same-audio / incomplete trials.

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

Live clone/recapture/canary require a connected stack with credentials and a
tested rollback; this release recommends only and never routes production traffic.

## Synthetic perturbations (`hotato/synth.py`)
Deterministic acoustic transforms of real fixtures (resample, gain, noise,
leakage, channel invert, silence, onset offset, clip), each carrying its parent
hash, recipe, seed, and an explicit synthetic designation. Synthetic and real
stay on separate axes: generated volume never raises the confidence of a real
recapture.

## What is deliberately not built yet
No public leaderboard, no opaque fleet-wide score, no model-generated label
becoming a contract without human approval, no production auto-deploy. Live
provider adapters (Vapi/Retell clone→apply→recapture) and canary routing are
gated on connected credentials and a tested rollback.
