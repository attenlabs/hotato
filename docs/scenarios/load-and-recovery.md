# My voice agent breaks under concurrent call load

Put a number on it: `hotato load telephony run` schedules calls against your
telephony provider in a closed-concurrency staircase, preserves one
verifiable evidence package per started call, and `hotato load telephony
verify` recomputes every metric offline from those packages, so "breaks under
load" becomes per-stage completion and drop rates with receipts instead of an
anecdote.

## The staircase, dry-run first

A load plan is one JSON file. This one steps concurrency 1, 2, 3 over six
calls using the built-in `local` provider (an in-process lifecycle fixture:
zero network, zero cost), so you can prove the plane end to end before a
single billable call:

```json
{
  "schema": "hotato.load-plan.v2",
  "id": "staircase-local",
  "call": {"schema": "hotato.telephony-call.v1", "id": "base", "provider": "local",
           "to": "+15550100", "agent_id": "agent-local", "phone_number_id": "phone-local"},
  "stages": [
    {"name": "step-1", "phase": "warmup", "model": "closed", "concurrency": 1, "calls": 1},
    {"name": "step-2", "phase": "spike", "model": "closed", "concurrency": 2, "calls": 2},
    {"name": "step-3", "phase": "recovery", "model": "closed", "concurrency": 3, "calls": 3}
  ],
  "slos": {"min_lifecycle_completion_rate": 1.0, "max_dropped_start_rate": 0.0}
}
```

A closed stage keeps a fixed number of calls in flight and starts a
replacement only after one finishes; an open stage records a
`generator_saturated` dropped start instead of silently queueing, so
coordinated omission stays visible.

```console
$ hotato load telephony run load-plan.json --out load-run
```

Trimmed from the result (the full JSON is written alongside the run):

```json
{
  "metrics": {
    "scheduled": 6, "started": 6, "created": 6,
    "lifecycle_completed": 6, "lifecycle_completion_rate": 1.0,
    "dropped_starts": 0, "dropped_start_rate": 0.0
  },
  "slos": [
    {"id": "max_dropped_start_rate", "observed": 0.0, "operator": "max", "status": "PASS", "threshold": 0.0},
    {"id": "min_lifecycle_completion_rate", "observed": 1.0, "operator": "min", "status": "PASS", "threshold": 1.0}
  ],
  "provider_completion_is_quality_pass": false
}
```

That last field is the scope statement in machine form: lifecycle completion
is signalling evidence that calls started and terminated, never a
conversation-quality verdict. Quality stays with the scoring commands.

## Per-child evidence, re-verified offline

The run directory holds one package per started call, plus the rollup:

```console
$ ls load-run
calls  observations.jsonl  report.html  summary.json  verification-plan.json
$ ls load-run/calls
000-000000  001-000000  001-000001  002-000000  002-000001  002-000002
```

Each child binds its receipts by digest and names the state of every evidence
lane explicitly (`calls/002-000002/manifest.json`, trimmed):

```json
{
  "schema": "hotato.load-call-package.v2",
  "stage": "step-3",
  "provider": "local",
  "lifecycle_status": "completed",
  "evidence": {
    "call_lifecycle": "PRESENT",
    "delivered_audio": "UNOBSERVABLE",
    "tool_trace": "UNOBSERVABLE",
    "backend_state": "UNOBSERVABLE"
  },
  "artifacts": {
    "create-receipt.json": "sha256:19d8de57...",
    "terminal-receipt.json": "sha256:a369f1ed..."
  }
}
```

`UNOBSERVABLE` is a load-bearing word: a lane the run could not observe says
so, and a bare claim can never satisfy an evidence SLO. Then any machine,
any time later, recomputes the whole rollup from the packages:

```console
$ hotato load telephony verify load-run
{
  "ok": true,
  "mismatches": [],
  ...
}
```

`verify` hashes every child and recomputes per-stage and aggregate metrics;
a tampered or missing package is a named mismatch, never a silent pass.

## Then point it at your provider

The same plan shape drives twilio, vapi, and retell. A remote plan requires
the v2 safety gates before the first provider request: a positive per-call
cost estimate, a total budget, a destination allowlist, a hard `max_calls`
ceiling, and an operator stop file, so a load test is cost-bounded by
construction. The full plan schema, the open arrival model, recovery
measurement, and the evidence-SLO semantics are in
[`docs/LOAD-AND-RECOVERY.md`](../LOAD-AND-RECOVERY.md); driving scripted
caller programs under load (the `hotato load caller` family) is
[`docs/CALLER-LOAD.md`](../CALLER-LOAD.md).
