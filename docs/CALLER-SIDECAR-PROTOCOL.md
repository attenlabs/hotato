# Caller sidecar protocol

`WebSocketCallerSession` is the concrete boundary between Hotato's bounded
caller program and a Pipecat, LiveKit, SIP, or provider adapter. The WebSocket
subprotocol is `hotato.caller.v1`. Loopback is the default; remote egress is an
explicit option and requires `wss://`.

The client sends a `hotato.caller-session.v1` hello with a random nonce and the
complete operation list. The sidecar returns a nonce-bound `ready` message,
adapter name/version, and one of `SUPPORTED`, `UNSUPPORTED`, or `UNOBSERVABLE`
for every operation. An omitted capability refuses the session.

Commands are canonical JSON envelopes with a contiguous sequence. PCM16LE is
announced by a `send_audio` command containing byte count and SHA-256, followed
by one binary message: the four bytes `HTC1`, a big-endian 32-bit command
sequence, and the exact PCM bytes.

Every command is synchronous at the protocol boundary. After accepting the
complete command (including the binary frame for `send_audio`), the sidecar
must return the matching sequence and command:

```json
{
  "schema": "hotato.caller-session.v1",
  "type": "command_result",
  "sequence": 4,
  "command": "send_audio",
  "status": "completed",
  "receipt": {
    "accepted_bytes": 6400,
    "accepted_sha256": "sha256:..."
  }
}
```

Status is `completed`, `unsupported`, or `error`. An absent, mismatched, or
failed result stops the caller program. Events that arrive while a command is
pending are retained in order for the next `receive` node; any still queued at
the terminal node are drained into `caller-result.json.events`. A command result
establishes that the sidecar accepted the operation. It does not establish
that a carrier or target agent received the media; that stronger claim needs a
target-boundary event carrying the delivered stream hash.

For caller-load delivery credit, the target or carrier boundary emits this
exact custom event (the caller engine adds `sequence` and `event_sha256`):

```json
{
  "schema": "hotato.caller-session.v1",
  "type": "event",
  "event": {
    "kind": "custom",
    "custom_type": "hotato.delivered-audio.v1",
    "authority": "target_boundary",
    "submitted_sha256": "sha256:<64 lowercase hex>",
    "delivered_sha256": "sha256:<64 lowercase hex>",
    "workload_child_id": "<run_context.child_id>",
    "workload_plan_sha256": "<run_context.workload_plan_sha256>"
  }
}
```

`authority` is exactly `target_boundary`, `target_participant_reported`, or
`carrier_boundary`. The submitted digest must identify PCM emitted by that
child. Every emitted PCM digest needs a valid receipt before the aggregate
reports delivery evidence `PRESENT`. Unknown fields, malformed identities,
replayed child/workload bindings, or partial coverage remain `MISSING`.
`delivered_sha256` identifies bytes observed by the reporting boundary; it is
not a packet trace or evidence about an uninstrumented downstream hop.

Incoming data is a JSON event envelope:

```json
{
  "schema": "hotato.caller-session.v1",
  "type": "event",
  "event": {"kind": "transcript", "text": "How can I help?"}
}
```

The caller engine accepts bounded transcript, tool-result, state-snapshot,
DTMF, lifecycle, transfer, hold, timing, timeout, and custom events. Sidecar
events are adapter evidence. A configured impairment receives delivery credit
only when the target boundary emits evidence for the bytes it received.

Handshake headers may carry credentials, but values are resolved inside the
worker and never serialized into multiprocessing factories, control messages,
or caller packages. Plaintext `ws://` is accepted only for loopback targets.
The strict WebSocket client refuses
redirects, extensions, remote addresses unless opted in, oversized messages,
invalid framing, and malformed JSON.

After the nonce-bound ready exchange, the session records
`connected_endpoint_sha256`, computed from the normalized credential-free
WebSocket URL. This binds remote load children to the configured endpoint. It
does not prove media delivery.
