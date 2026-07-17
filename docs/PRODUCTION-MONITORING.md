# Production monitoring: durable evidence into a regression candidate

Hotato's production plane accepts bounded call events, preserves ambiguity, and
turns a finalized session into an offline-verifiable regression candidate. It
is a single-process, self-hosted evidence assembler backed by SQLite WAL. Put a
durable OpenTelemetry Collector or message queue in front when a deployment
needs ingress buffering across process restarts. The included
[`deploy/control-plane`](../deploy/control-plane/README.md) bundle does this
with Collector Contrib and a local persistent queue. That queue survives a
process restart; it does not replicate data across hosts.

The implementation lives in `hotato.production`. It has no runtime dependency
beyond Python's standard library.

The gateway accepts OTLP/HTTP JSON traces at the standard `/v1/traces` path
and at `/v1/otlp/traces`. Protobuf input is refused rather than guessed.
Authentication is required on this boundary. The deployed Collector accepts
standard OTLP/gRPC and OTLP/HTTP protobuf or JSON on loopback, converts trace
batches to JSON, and authenticates the private hop to this gateway.
After its database transaction commits, `/v1/traces` returns the standard empty
`ExportTraceServiceResponse` JSON object (`{}`). The Hotato-specific
`/v1/otlp/traces` compatibility path returns the richer per-event receipt.

## The write contract

The gateway follows this order for every request:

1. bound `Content-Length` before reading;
2. authenticate the exact body bytes with bearer token or HMAC;
3. parse and validate the event;
4. enter an SQLite `BEGIN IMMEDIATE` transaction;
5. persist the event, session update, and audit-chain entry;
6. commit with `PRAGMA synchronous=FULL`;
7. return a receipt containing `"durability": "committed"`.

A storage error returns `503`. A validated event never receives a success ACK
before its transaction commits. The HTTP server admits a bounded number of
workers and returns `503` with `Retry-After: 1` when capacity is full. There is
no hidden in-memory queue.

## Start the gateway

For an operated service, declare the maintenance loop as data instead of
depending on an operator to remember three separate commands:

```json
{
  "schema": "hotato.production-maintenance.v1",
  "interval_seconds": 30,
  "quiescence_seconds": 30,
  "required_lanes": [
    "participant_audio",
    "transcript",
    "model_trace",
    "tool_calls",
    "backend_state"
  ],
  "alert_rules": [
    {"id": "degraded-session", "condition": "degraded"},
    {"id": "event-conflict", "condition": "conflict"},
    {"id": "missing-audio", "condition": "missing_audio"}
  ],
  "retention_seconds": 2592000
}
```

Run one inspectable cycle:

```bash
hotato production maintain maintenance.json \
  --db .hotato/production.sqlite3 --format json
```

Or run the authenticated gateway and maintenance loop together while keeping
the bearer value out of argv and the container environment:

```bash
hotato production serve \
  --db .hotato/production.sqlite3 \
  --token-file /run/secrets/hotato_production_token \
  --maintenance-policy maintenance.json
```

Each cycle finalizes eligible sessions first, evaluates persisted alert state
second, and enforces retention last. A cycle failure is retained in supervisor
status and retried at the next interval; it never receives a success ACK from
the ingest gateway because ingest durability is a separate transaction. The
supervisor does not create assertions, score calls, export regression
candidates, or send notifications.

The lower-level Python gateway remains available:

```python
from hotato.production import ProductionGateway, ProductionStore

store = ProductionStore(".hotato/production.sqlite")
gateway = ProductionGateway(
    store,
    token="replace-with-at-least-16-characters",
    hmac_secret="replace-with-at-least-32-characters",
    host="127.0.0.1",
    port=8099,
    max_workers=16,
)

try:
    gateway.thread.join()
finally:
    gateway.close()
    store.close()
```

Binding to a non-loopback address exposes an authenticated HTTP service. Place
TLS termination in front of it and rotate credentials through the surrounding
secret manager.

## Event envelope

Every event conforms to
[`production-event.v1.json`](../src/hotato/schema/production-event.v1.json).
The envelope is CloudEvents-shaped and adds required execution authority:

```json
{
  "specversion": "1.0",
  "id": "tool-result-00017",
  "source": "livekit-sidecar",
  "type": "tool.result",
  "subject": "call-01J2Q1N4WQ",
  "time": "2026-07-17T12:00:00.123Z",
  "sequence": 17,
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": {
    "availability": "available",
    "tool": "cancel_subscription",
    "result": {"status": "failed"}
  },
  "authority": {
    "kind": "adapter_reported",
    "eligible_for_execution_claim": false
  }
}
```

Supported event types are enumerated in the schema. Unknown types and unknown
top-level fields are refused. Each event is capped at 8 MiB.

### Availability and authority remain separate

Evidence availability is one of:

- `available`: the event carries or references evidence;
- `unavailable`: the producer expected the evidence and could not obtain it;
- `unsupported`: the producer cannot provide that evidence lane;
- `missing`: no event has yet described the lane.

Authority is one of:

- `measured`;
- `signed_attestation`;
- `provider_export`;
- `adapter_reported`;
- `submitted`.

Only `measured` and `signed_attestation` may set
`eligible_for_execution_claim: true`. Request authentication establishes who
submitted the envelope. It never upgrades an adapter or provider export into an
independent execution measurement.

The session manifest reports five independent evidence lanes:

- participant audio;
- transcript;
- model trace;
- tool calls;
- backend state.

No lane substitutes for another. The manifest has no blended score.

## Authentication

### Bearer

```bash
curl -sS http://127.0.0.1:8099/v1/events \
  -H 'Authorization: Bearer replace-with-at-least-16-characters' \
  -H 'Content-Type: application/json' \
  --data-binary @event.json
```

### HMAC

HMAC signs the exact bytes sent in the request:

```text
hex(HMAC-SHA256(secret, decimal_unix_timestamp + "." + raw_body))
```

Send the result as `X-Hotato-Signature: v1=<hex>` and the timestamp as
`X-Hotato-Timestamp`. The default acceptance window is 300 seconds. Event
identity deduplication makes an accepted replay idempotent.

```python
import hashlib, hmac, json, time, urllib.request

secret = b"replace-with-at-least-32-characters"
raw = json.dumps(event, separators=(",", ":")).encode()
timestamp = str(int(time.time()))
signature = hmac.new(secret, timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
request = urllib.request.Request(
    "http://127.0.0.1:8099/v1/events",
    data=raw,
    headers={
        "Content-Type": "application/json",
        "X-Hotato-Timestamp": timestamp,
        "X-Hotato-Signature": "v1=" + signature,
    },
)
urllib.request.urlopen(request).read()
```

## Duplicate, conflict, and ordering behavior

Event identity is `(source, id)`.

| Arrival | Stored result | Session effect |
|---|---|---|
| unseen identity | `stored` | event count and evidence update |
| same identity, same canonical bytes | `duplicate` | duplicate counter increments |
| same identity, different canonical bytes | HTTP `409` | original event remains; conflict is recorded; session becomes `DEGRADED` |
| sequence lower than or equal to the source cursor | `out_of_order` | event remains stored; out-of-order counter increments |
| no sequence | `stored` | unsequenced counter increments; finalization is `DEGRADED` |

Sequence cursors are scoped to `(subject, source)`, so independent producers may
each begin at sequence zero. Arrival order remains in the event export. Hotato
does not discard or silently reorder late observations.

An event arriving after session finalization remains stored and changes the
manifest to `DEGRADED` with `late_event_after_finalization`. The previously
exported candidate remains content-addressed and unchanged.

## Finalization

`session.ended` moves a session to `QUIESCENT`. Call `finalize()` after the
declared quiescence period:

```python
manifests = store.finalize(quiescence_seconds=30)
```

By default, a session becomes `COMPLETE` only when exactly one
`session.started` and one `session.ended` event were observed, all five evidence
lanes are available, no identity conflict occurred, and no out-of-order event
was observed. Every event must also carry a sequence from its source. Lifecycle
and unsequenced counts remain explicit in the manifest. A fixed
workflow that does not produce every evidence lane can declare its required
subset at finalization:

```python
manifests = store.finalize(
    quiescence_seconds=30,
    required_lanes=("participant_audio", "transcript", "backend_state"),
)
```

The selected list is persisted in `required_evidence_lanes`; it cannot disappear
from the exported manifest. Every other finalized session is `DEGRADED`; the
manifest identifies each unavailable or missing lane and the finalization
reason.

Finalization measures evidence completeness. It does not decide whether the
agent passed a product assertion.

## OpenTelemetry trace bridge

`POST /v1/traces` and `POST /v1/otlp/traces` accept bounded OTLP/HTTP JSON with
the standard `resourceSpans -> scopeSpans -> spans` shape. Use
`X-Hotato-Source` to name the sidecar. The former returns `{}` for OTLP client
compatibility after commit; the latter returns Hotato's normalized result and
durability fields. Each span must carry one of these attributes:

- `hotato.session_id`;
- `session.id`;
- `conversation.id`;
- `call.id`.

Optional attributes:

- `hotato.event_type`: one supported production event type;
- `hotato.sequence`: integer ordering within that source;
- `hotato.evidence.availability`: `available`, `unavailable`, or `unsupported`.

The bridge preserves trace ID, span ID, parent span ID, resource attributes,
scope, status, and nanosecond timestamps. It assigns `adapter_reported`
authority. A caller may use `normalize_otlp_json()` directly and select a
different authority, subject to the same structural validation.

The OTLP endpoint validates the complete span set before opening its transaction
and commits the normalized batch atomically. Identity conflicts are returned as
per-event results while preserving the original event bytes.

### Included Collector boundary

The control-plane deployment renders these local-only inputs:

```text
OTLP/gRPC          127.0.0.1:4317
OTLP/HTTP   http://127.0.0.1:4318
```

The Collector uses the stable `otlp_http` exporter name, JSON encoding, and no
compression for its private hop to `127.0.0.1:8432/v1/traces`. The Hotato bearer
credential exists only in the OTel-specific mode-`0400` config volume. It is
absent from the Collector environment, process arguments, and bootstrap
manifest.

Exporter requests are staged in an fsyncing `file_storage` queue before
delivery. The fixed queue has a capacity of 10,000 requests and retries an
enqueued request without an elapsed-time cutoff. This is bounded, single-host
buffering:

- queue units are exporter requests, not spans, calls, or bytes;
- the storage volume has no application-level byte quota;
- storage compacts at startup and after its allocated database exceeds 100 MiB
  and later drains below 10 MiB; compaction itself needs temporary disk
  headroom;
- when the queue is full, the disk is full, or storage returns an I/O error,
  enqueue can fail and telemetry can be dropped;
- Collector acceptance is not a Hotato SQLite commit receipt;
- deleting or losing the host volume loses any request that has not drained.

The Collector exposes its own metrics only on
`http://127.0.0.1:8888/metrics`. Monitor
`otelcol_exporter_queue_size`, `otelcol_exporter_queue_capacity`,
`otelcol_exporter_enqueue_failed_spans`,
`otelcol_exporter_send_failed_spans`, receiver-refused spans, process restarts,
and disk space. Treat any nonzero enqueue-failure delta as an evidence-loss
incident. Run restart, gateway-outage, queue-capacity, and disk-exhaustion drills
against the exact pinned image digest before qualifying a deployment.

The rendered settings follow the pinned Collector `v0.153.0` contracts for the
[OTLP HTTP exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.153.0/exporter/otlphttpexporter/README.md),
[exporter helper persistent queue](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.153.0/exporter/exporterhelper/README.md),
and Contrib
[file storage extension](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.153.0/extension/storage/filestorage/README.md).

This endpoint is a trace normalizer. The included Collector pipeline also wires
traces only; application metrics and logs require a separately declared
pipeline and backend. The Prometheus endpoint above contains the Collector's
own operational metrics, not application telemetry.

## Alerts

The local alert engine persists each transition and an audit-chain entry:

```python
changes = store.evaluate_alerts([
    {"id": "evidence-complete", "condition": "incomplete_evidence"},
    {"id": "event-conflict", "condition": "conflict"},
])
```

Supported conditions:

- `degraded`;
- `missing_audio`;
- `missing_tool_evidence`;
- `incomplete_evidence`;
- `conflict`;
- `out_of_order`;
- `unsequenced`.

Transitions use `FIRING` and `RESOLVED`. A resolved alert that fires again gets
a new generation and opened timestamp. Stable alert states produce no duplicate
transition.

## Prometheus

`GET /metrics` requires bearer authentication. Event, duplicate, conflict, and
ordering-anomaly counters carry no labels. Session `status` and alert `state`
use fixed enumerations. Session IDs, rule IDs, providers, phone numbers, and
trace IDs never become labels, preventing unbounded series cardinality.
Counters live in a dedicated SQLite table and remain monotonic when retention
deletes event/session rows.

## Promote a production failure

Finalize the session, then export it:

```python
result = store.export_regression_candidate(
    "call-01J2Q1N4WQ",
    "fixtures/candidates/call-01J2Q1N4WQ",
)
```

The exporter writes to an owned staging directory, fsyncs the files, verifies
all declared byte counts and SHA-256 digests, and atomically renames the
directory into place. It refuses to overwrite an existing path.

The output contains:

```text
call-01J2Q1N4WQ/
├── candidate.json
└── events.jsonl
```

`candidate.json` conforms to
[`production-regression-candidate.v1.json`](../src/hotato/schema/production-regression-candidate.v1.json).
Its status is `CANDIDATE`: production evidence alone does not invent an expected
outcome or become a CI gate. A reviewer authors the assertion and chooses which
evidence may enter a shareable fixture.

Verify on a clean, offline machine:

```python
from hotato.production import verify_regression_candidate

result = verify_regression_candidate("fixtures/candidates/call-01J2Q1N4WQ")
assert result["valid"]
```

Payload persistence is default-deny at ingest. Each event type has a narrow
allowlist for structural values such as evidence availability, validated
content digests, timing counters, and bounded status enums. Every other scalar,
object, or array is replaced as a whole by `redacted: true`, its canonical
`byte_count`, and no value-derived digest. Omitting the digest prevents an
offline dictionary attack against low-entropy redacted values. An allowlisted
field with the wrong value type is reduced to the same descriptor. This
preserves portable monitoring structure without assuming an unfamiliar field
is safe.
Field names and the event envelope remain visible, so identifiers still need
review before a candidate is shared. A caller can explicitly use
`store.ingest(event, redact_payloads=False)` inside its own boundary; the
session manifest and candidate then declare `payload_storage`/`payloads` as
`unredacted` or `mixed` instead of claiming redaction. Review remains required
before sharing because envelope identifiers and allowlisted structural metadata
may still be organization-specific.

## Audit and retention

Verify the append-only audit hash chain:

```python
assert store.verify_audit_chain()["valid"]
```

Audit targets are deterministic SHA-256 digests from their first write, so the
raw provider identifier does not remain in the audit row after retention.
These digests are integrity identifiers, not an anonymity guarantee: a
low-entropy identifier can be guessed offline. Chain verification returns the
first invalid sequence if a row changes and compares the computed head/count
with a local metadata checkpoint to detect tail truncation. An administrator
who can rewrite the entire database can rewrite both the chain and its local
checkpoint; export the returned head digest to an external append-only system
when that threat is in scope.

Delete one session:

```python
receipt = store.delete_session("call-01J2Q1N4WQ")
```

Apply a local retention window to finalized sessions:

```python
receipts = store.enforce_retention(retention_seconds=30 * 24 * 60 * 60)
```

Deletion removes events, conflicts, sequence cursors, session state, alerts, and
their transitions. A pseudonymous deletion receipt and the pseudonymous audit
chain remain. The receipt records the pre-deletion manifest digest and deleted
event count. Database backups and upstream collectors need their own retention
policy; this process cannot delete copies outside its store.

## Operational boundary

The included implementation has hermetic tests for authentication, exact-byte
HMAC verification, commit visibility from a second connection, duplicate and
conflict behavior, source-scoped ordering, evidence authority, finalization,
late events, alert generations, bounded-label metrics, OTLP correlation,
candidate tamper detection, audit tamper detection, and retention receipts.

Those tests establish the local contract. They do not establish a public
throughput, recovery-time, multi-region, or availability number. Measure those
properties in the intended deployment with a frozen load schedule and publish
the environment and raw results before making an operational claim.
