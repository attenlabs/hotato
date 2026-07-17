# Load and recovery with portable evidence

Hotato schedules calls in either a closed concurrency model or an open arrival
model. It preserves one verifiable child package per started call. Provider
completion, delivered-media evidence, tool/state evidence, scheduling loss,
and recovery remain separate measurements.

```json
{
  "schema": "hotato.load-plan.v2",
  "id": "refund-release",
  "call": {
    "schema": "hotato.telephony-call.v1",
    "id": "base",
    "provider": "vapi",
    "to": "+15551234567",
    "agent_id": "agent-id",
    "phone_number_id": "phone-id"
  },
  "stages": [
    {"name": "warm", "phase": "warmup", "model": "closed", "concurrency": 2, "calls": 10},
    {"name": "spike", "phase": "spike", "model": "open", "arrival_rate_per_second": 4, "duration_seconds": 30, "max_in_flight": 40},
    {"name": "recover", "phase": "recovery", "model": "closed", "concurrency": 4, "calls": 20}
  ],
  "safety": {
    "max_calls": 150,
    "estimated_cost_per_call_usd": 0.05,
    "max_estimated_cost_usd": 7.50,
    "allowed_destinations": ["+1555"],
    "stop_file": ".hotato/STOP"
  },
  "slos": {
    "max_dropped_start_rate": 0.01,
    "max_p95_scheduling_delay_seconds": 0.25,
    "min_lifecycle_completion_rate": 0.99,
    "min_evidence_complete_rate": 0.99
  }
}
```

An open stage does not queue work when its generator is saturated. It records a
`generator_saturated` dropped start so coordinated omission remains visible.
Closed stages keep a fixed number of calls in flight and start a replacement
only after one finishes.

The safety block is evaluated before a provider call. Twilio, Vapi, and Retell
workloads require a v2 plan with a positive per-call estimate, a positive total
budget, and a non-empty destination allowlist. Omitting any one refuses the
workload before the first provider request. Destination prefixes, a hard call
ceiling, and an operator stop file bound execution. Hotato performs zero
automatic create retries because the provider-neutral interface has no shared
idempotency guarantee.

Each `calls/*/manifest.json` binds redacted lifecycle receipts and any evidence
export to the provider, provider-namespaced call pseudonym, normalized call
digest, and normalized workload digest. The four evidence states are
`PRESENT`, `MISSING`, `UNSUPPORTED`, and `UNOBSERVABLE`. `PRESENT` requires a
content digest and an explicit authority for every lane; a bare string cannot
satisfy an evidence SLO. `min_evidence_complete_rate` is stricter than artifact
presence: all three non-lifecycle lanes must be `PRESENT` and carry either
`measured` or `signed_attestation` authority, with
`eligible_for_execution_claim=true`. Provider-, sidecar-, target-, and
unverified reports remain portable evidence but cannot satisfy that execution
evidence SLO. A provider-reported completed call can satisfy lifecycle
completion while evidence completeness remains zero.

`hotato load telephony verify DIR` hashes every child and recomputes per-stage and aggregate
metrics, recovery windows, SLO rows, the HTML report, overall status, and exit
code from `observations.jsonl` plus the bound, non-secret
`verification-plan.json`. It rejects missing/duplicate observations, swapped
child references, extra artifacts, invalid evidence states, and a summary that
has merely been rehashed after its verdict changed. No network or model is
required.

The package hashes establish internal identity and consistency. They do not
authenticate who produced a directory: a party able to replace every artifact
can construct a different, internally consistent package. Preserve the package
in an access-controlled artifact store or attach an external organization
signature before using publisher identity as a trust decision.

Fault schedules require a call controller that exposes the named injection.
Unsupported injection produces an explicit call error; it is never simulated
silently. Recovery is measured from the end of the declared fault window to the
first later successful lifecycle observation and stays separate from quality.
