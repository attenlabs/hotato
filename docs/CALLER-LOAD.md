# Caller-program load runner

`hotato.caller_load` executes the same bounded caller program many times without
discarding the evidence from any started invocation. It is the load plane for
Hotato's caller engine, rather than a shortcut that treats an HTTP response or
a completed call as a quality pass.

The runner supports two workload models:

- **closed concurrency** keeps up to `concurrency` caller invocations active
  until an exact call count has been attempted;
- **open arrival** schedules starts against a monotonic arrival clock. When the
  target rate exceeds `max_in_flight`, the scheduled start is recorded as
  `DROPPED`. The scheduler does not wait and silently shift that call later.

Every started invocation runs in its own operating-system process and writes a
normal `hotato.caller-package.v1` package. The aggregate package binds each
child to the workload-plan hash, stage ID, stage model, and invocation index.
This lets an offline verifier detect a child copied from another stage or run.

## Minimal plan

```json
{
  "schema": "hotato.caller-load-plan.v1",
  "id": "refund-release-candidate",
  "caller_plan": {
    "schema": "hotato.caller-plan.v1",
    "id": "refund-caller",
    "mode": "scripted",
    "start": "ask",
    "nodes": [
      {"id": "ask", "type": "say", "text": "Please refund order 42.", "next": "done"},
      {"id": "done", "type": "hangup", "reason": "scenario_complete"}
    ],
    "limits": {"max_duration_ms": 30000, "max_cost_microusd": 0}
  },
  "stages": [
    {"id": "warm", "model": "closed", "concurrency": 4, "calls": 20},
    {
      "id": "arrival-spike",
      "model": "open",
      "arrival_rate_per_second": 10,
      "duration_seconds": 30,
      "max_in_flight": 50
    }
  ],
  "safety": {
    "max_calls": 320,
    "max_concurrency": 50,
    "max_call_duration_ms": 30000,
    "max_start_delay_ms": 250,
    "max_cost_per_call_microusd": 0,
    "max_cost_microusd": 0,
    "stop_file": "./STOP-HOTATO-CALLER-LOAD"
  },
  "slos": {
    "max_dropped_start_rate": 0.01,
    "min_completion_rate": 0.99,
    "max_blocked_error_rate": 0.01,
    "min_child_verification_rate": 1.0,
    "min_evidence_complete_rate": 0.99,
    "max_p95_scheduling_delay_ms": 100
  }
}
```

Open stages gain a normalized, read-only `calls` field equal to the number of
scheduled arrival slots. The stored `workload-plan.json` contains that
normalized value.

## Adapter boundary

The runner does not put credentials in the workload plan. Supply credentials
through a factory closure or a secret manager. Every API call declares its
session-factory transport scope; Hotato cannot infer what arbitrary Python
factory code opens.

```python
from hotato import caller_load
from my_project.sessions import new_session

plan = caller_load.load_plan("caller-load.json")

def session_factory(run_context):
    # run_context is safe to attach to transport receipts and traces.
    # It contains child_id, workload_plan_sha256, stage_id, stage_model,
    # invocation_index, and expected_session_boundary. Fetch credentials
    # outside the plan.
    return new_session(
        target_url="ws://127.0.0.1:9000/voice",
        token_from_secret_manager=True,
        evidence_labels=run_context,
    )

run = caller_load.run_caller_load(
    plan,
    "./hotato-caller-load-run",
    session_factory,
    execution_scope="local",
)
raise SystemExit(run.exit_code)
```

`model_factory(context)` and `tts_factory(context)` use the same contract and
are optional. A `generative` caller plan requires `model_factory`. Each factory
runs inside the child process. On platforms whose multiprocessing runtime uses
`spawn`, factories must be pickleable module-level callables.

Session and model credentials can still leak if an adapter deliberately writes
them into its `evidence()` output. Adapter authors must return receipts and
content hashes, never bearer tokens, authorization headers, or signed URLs.

## Safety contract

Validation happens before a child process starts:

- scheduled calls cannot exceed `safety.max_calls`;
- stage concurrency cannot exceed `safety.max_concurrency`;
- the supervisor terminates a worker after `max_call_duration_ms` and writes a
  separate verified `ERROR` child package for that invocation;
- caller-plan duration and model-cost ceilings are narrowed to the workload
  ceilings for each child;
- `scheduled_calls × max_cost_per_call_microusd` must fit inside
  `max_cost_microusd` before execution;
- the stop file prevents every subsequent scheduled start. Those rows are
  `STOPPED`, not silently omitted, and the aggregate status is `INCONCLUSIVE`.

The cost bound depends on model adapters reporting usage correctly to the
caller engine. A provider with unknown or unbounded spend must not be assigned
a zero cost ceiling. On timeout, the worker attempts `session.hangup()` and an
optional `session.close()` before the supervisor escalates from termination to
a process kill. Carrier/provider billing and teardown remain external evidence;
an adapter that ignores teardown cannot be made cost-bounded by a local process
limit alone.

## Coordinated-omission evidence

Each scheduled row records:

- its fixed `scheduled_offset_ms`;
- observed `scheduling_delay_ms`;
- `STARTED`, `DROPPED`, or `STOPPED` disposition;
- `MAX_IN_FLIGHT` or `START_DELAY` for a dropped open-arrival start.

Open-arrival capacity pressure is therefore visible as dropped work. The
scheduler does not convert a 10 calls/second workload into a slower closed loop
whose latency distribution looks healthier.

## Separate result lanes

The aggregate and every stage report these values independently:

1. `scheduled`, `started`, `dropped_starts`, and `stopped_before_start`;
2. caller status counts: `completed`, `hung_up`, `blocked`, and `error`;
3. caller child-package `verified` and `unverified` counts;
4. delivered-evidence states: `present`, `missing`, `unsupported`, and
   `unobservable`;
5. remote session-endpoint bindings: `matched`, `missing`, `mismatch`, and
   `not_required`;
6. scheduling-delay and invocation-duration distributions;
7. reported model cost in micro-USD.

`COMPLETED` and `HUNG_UP` mean the caller program reached a terminal state.
They do not mean the target agent produced the right outcome. `PRESENT`
delivery evidence means the session explicitly reported target-bound evidence;
it is also not an outcome verdict. The schema fixes both statements and fixes
`blended_score` to `null`.

## Offline verification

```python
from hotato.caller_load import verify_caller_load

verification = verify_caller_load("./hotato-caller-load-run")
assert verification["ok"], verification["mismatches"]
```

Verification performs no network, model, TTS, session, or provider call. It:

- uses bounded no-follow reads and refuses FIFOs and symlinks;
- verifies the full aggregate file manifest and exact package layout;
- verifies every started child with `caller.verify_package`;
- rebuilds the expected child caller plan and checks its workload/stage/index
  identity;
- recomputes child status, exit code, evidence state, and reported cost from the
  child package;
- recomputes aggregate and per-stage metrics, every SLO, aggregate status, and
  exit code;
- rejects rehashed status/exit forgeries, child swaps, and extra files.

Exit codes are `0` for `PASS`, `1` for `FAIL`, and `2` for `INCONCLUSIVE`.
Only declared SLOs gate `PASS`/`FAIL`; the runner never invents a blended score
or an undeclared quality threshold.

`delivery_evidence=PRESENT` has a narrow contract. Credit can come from either
an exact `session_boundary.delivery_evidence` receipt or an exact
`hotato.delivered-audio.v1` custom child event. Both forms require an authority
of `target_boundary`, `target_participant_reported`, or `carrier_boundary`, the
workload and child IDs from the supplied run context, and both
`submitted_sha256` and `delivered_sha256`. The submitted digest must match PCM
emitted by that child; every emitted PCM digest must be covered. The event form
is documented in `CALLER-SIDECAR-PROTOCOL.md`. A malformed claim, generic
digest, configured impairment hash, SDK submission receipt, partial receipt
set, or summary state cannot earn delivery credit. Those cases remain
`MISSING` or `UNOBSERVABLE`. This is reporting-boundary evidence, not a packet
trace or proof about a later uninstrumented hop.

Remote sidecar load is default-deny. `--allow-remote` is accepted only when
the normalized workload plan contains an exact credential-free `wss://`
endpoint, a remote call ceiling covering the schedule, the fixed
`I_ACCEPT_REMOTE_CALL_SIDE_EFFECTS_AND_UNOBSERVABLE_EXTERNAL_COST`
acknowledgement, and `external_cost_state=UNOBSERVABLE`. Queries and embedded
credentials are refused. The result separates the plan-declared endpoint
digest from the runtime-configured endpoint digest. Every started child also
must return the same `connected_endpoint_sha256` from its successfully
constructed session boundary. `metrics.session_endpoint_binding` exposes
matched, missing, and mismatched children. Any missing or mismatch makes the
aggregate `INCONCLUSIVE` with exit code 2, even when quality SLOs pass; the
offline verifier recomputes that state from each child. The result records
external provider cost as `null`/`UNOBSERVABLE`; `actual_cost_microusd` and its
bound cover caller-model-reported cost only. A remote provider bill never
silently appears as zero.

## Package layout

```text
hotato-caller-load-run/
├── workload-plan.json
├── observations.jsonl
├── result.json
├── package-manifest.json
└── children/
    └── <child-id>/
        ├── caller-plan.json
        ├── caller-result.json
        ├── package-manifest.json
        └── artifacts/...
```

The aggregate manifest hashes every byte-bearing file, including each child
manifest. Child packages retain the caller engine's model, TTS, text, PCM,
event, session-boundary, and authority evidence.

## Scope boundary

This module exercises caller programs against a supplied session adapter. It
does not provision SIP trunks, carrier capacity, media relays, speech models,
or production alerting. Those components remain independently observable so a
provider lifecycle response cannot substitute for target-delivered media or a
target outcome.
