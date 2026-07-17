# Transport runtime

Hotato separates three facts that voice-test systems often collapse:

1. a provider accepted or completed a call lifecycle operation;
2. a media/signalling runtime delivered bytes or events at an agent boundary;
3. the resulting conversation satisfied outcome, policy, timing, and reliability assertions.

The first fact comes from `TelephonyClient`. The second comes from a
`ConversationSession` implementation. The third comes from Hotato's evidence and scoring
lanes. A lifecycle status never substitutes for delivered-media evidence or task outcome.

## Runtime contracts

`hotato.call_runtime.CallController` is the lifecycle boundary:

```text
capabilities(provider)
create(spec) -> handle
get(provider, call_id) -> handle
wait(handle) -> terminal handle
cancel(handle) -> handle
export(handle, output_dir) -> redacted receipt path
cleanup(handle, export_path) -> local deletion receipt
```

`hotato.call_runtime.ConversationSession` is the duplex media/signalling boundary:

```text
capabilities()
connect()
events()
send_audio(pcm_s16le, sample_rate_hz, channels)
send_dtmf(digits)
hold(enabled)
transfer(destination, warm=False)
hangup()
close()
```

These are Python protocols. A provider SDK, local WebSocket harness, or separately operated
sidecar can implement them without being imported into Hotato's zero-dependency core.

Every capability has one explicit state:

| State | Meaning |
|---|---|
| `SUPPORTED` | The selected implementation executes the operation and returns the evidence its contract requires. |
| `UNSUPPORTED` | The selected implementation cannot execute the operation. |
| `UNOBSERVABLE` | The operation may occur elsewhere, but this boundary cannot return the evidence required to credit it. |

Unknown support does not become a failed quality verdict. `require_capability()` refuses both
`UNSUPPORTED` and `UNOBSERVABLE` before an operation starts.

## Normalized call events

Every media/signalling implementation emits `hotato.call-event.v1` records. Records form an
append-only SHA-256 chain per `(run_id, call_id)`:

```json
{
  "schema": "hotato.call-event.v1",
  "event_id": "agent-audio-0001",
  "run_id": "run-2026-07-17-001",
  "call_id": "call-01",
  "leg_id": "agent",
  "sequence": 0,
  "source": "livekit-sidecar",
  "kind": "audio.delivered",
  "observed_monotonic_ns": 321000000,
  "source_timestamp": "2026-07-17T12:00:00.321Z",
  "trace_id": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "payload": {"sample_rate_hz": 16000, "channels": 1, "bytes": 640},
  "raw_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "trust": "measured",
  "previous_event_hash": null,
  "event_hash": "sha256:<canonical-event-body-hash>"
}
```

`normalize_call_event()` rejects unknown fields, non-canonical JSON values, oversized events,
invalid digests, sequence gaps, clock reversal, run/call changes, and a mismatched claimed hash.
`AppendOnlyCallLog` rejects duplicate event IDs and verifies the entire chain.

`raw_sha256` hashes the bytes observed at the stated boundary. It does not claim those bytes
were heard by a participant. `trust` identifies the authority:

- `measured` for bytes or state measured at the boundary;
- `provider_reported` for provider lifecycle/API statements;
- `sidecar_reported` for an external runtime's report;
- `derived` for a deterministic transform over retained inputs;
- `model_reported` for a model output;
- `operator_attested` for an operator statement;
- `unverified` when no stronger authority is available.

## Lifecycle controller matrix

`hotato.telephony.TelephonyClient` controls provider lifecycle APIs. It does not transport media.

| Operation | local | Twilio | Vapi | Retell |
|---|---:|---:|---:|---:|
| create | supported | supported | supported | supported |
| status endpoint | unsupported | supported | supported | supported |
| wait for terminal status | supported | supported | supported | supported |
| cancel | supported | supported | unsupported | unsupported |
| redacted Hotato receipt export | supported | supported | supported | supported |
| delete matching local export | supported | supported | supported | supported |
| delete provider history | unsupported | unsupported | unsupported | unsupported |
| media / DTMF / hold / transfer | unobservable | unobservable | unobservable | unobservable |

Credentials come only from environment variables:

| Provider | Variables |
|---|---|
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| Vapi | `VAPI_API_KEY` |
| Retell | `RETELL_API_KEY` |

Receipts retain request field names and body byte counts. The provider response
uses a fixed lifecycle allowlist; every omitted response is bound only by its
canonical byte count and SHA-256. Free-form reason strings, identifiers,
telephone numbers, transcripts, recording URLs, messages, metadata,
authorization fields, keys, tokens, secrets, and passwords are never copied
from a provider response. Portable receipts and exports retain a provider-
domain-separated call-ID digest instead of the provider call ID. Export uses exclusive
file creation. Cleanup deletes only an export whose provider, call-ID digest,
and lifecycle receipt ID match the supplied in-memory handle. It never deletes
provider history.

## Media and signalling sidecars

Hotato composes established transport runtimes instead of implementing RTP, SIP, codec
negotiation, NAT traversal, or WebRTC congestion control.

### LiveKit SIP

`livekit_sip_contract(endpoint)` declares the evidence boundary for a LiveKit Server + LiveKit
SIP deployment. DTMF, hold, and transfer begin as `UNOBSERVABLE`; a session adapter promotes each
operation to `SUPPORTED` only when it returns the corresponding room/participant/SIP events and
delivered-audio digest. The expected evidence set is:

- room and participant events;
- SIP lifecycle/status events;
- delivered PCM or encoded-stream digest at the agent input boundary;
- per-leg event timestamps and trace correlation.

### Pipecat

`pipecat_media_contract(endpoint)` declares a streaming media boundary. The generic contract
requires pipeline events and a delivered-audio digest. DTMF, hold, and transfer are unsupported
until a concrete transport adapter defines and evidences them.

### SIPp

`SippSubprocessAdapter` executes a caller-supplied SIPp XML scenario for SIP-path and load probes.
SIPp owns SIP/RTP behavior. Hotato applies a static scenario policy, fixes the
subprocess argv and environment, and records receipts. The adapter does not
provide an operating-system sandbox.

```python
from hotato.call_runtime import SippSubprocessAdapter

receipt = SippSubprocessAdapter().run(
    {
        "target": "127.0.0.1:5060",
        "scenario_path": "fixtures/inbound.xml",
        "calls": 20,
        "rate_per_second": 2,
        "timeout_seconds": 180,
    },
    "artifacts/sipp-run-01",
)
```

The adapter:

- uses an argv array with `shell=False`;
- accepts no free-form extra arguments;
- validates the target and binds a regular, non-symlink XML file to its opened inode;
- parses XML with DTDs, entity declarations, external entities, and processing
  instructions disabled;
- defaults to a safe scenario profile that rejects every `<exec>` action,
  scenario file attribute, unsafe path attribute, and SIPp `[file ...]` host-file keyword;
- rejects `<setdest>` in the safe profile so scenario XML cannot bypass the
  resolved target and remote-IP policy;
- bounds calls, rate, scenario bytes, timeout, stdout, and stderr;
- supplies only `PATH`, `HOME`, and locale variables to the child;
- writes data artifacts with exclusive creation and mode `0600`, and stages
  the executable copy as mode `0700`;
- hashes the target instead of writing it into the receipt;
- reports process exit as `PASS` or `FAIL` without converting it into conversation quality.

Non-loopback SIP is default-deny. A remote spec must set `allow_remote=true`,
list the resolved destination IP in `remote_ip_allowlist`, cap the run with
`max_remote_calls`, and supply
`I_ACCEPT_REMOTE_SIP_SIDE_EFFECTS_AND_UNOBSERVABLE_EXTERNAL_COST`. The adapter
resolves once, passes the selected allowlisted IP to SIPp, and binds that
destination into the receipt. External SIP/carrier cost remains `null` and
`UNOBSERVABLE`. The built-in subprocess path stages and hashes the SIPp
executable; injected test runners report executable identity as unobservable.

The remote acknowledgement authorizes network side effects only. It does not
authorize scenario-level host commands, host-file reads, or destination
redirection.

### Trusted SIPp scenarios

SIPp XML is executable input. Its `<exec command="...">` action invokes a host
shell, while media actions and scenario keywords can read host files. Those
features are denied by default. A deployment that has reviewed the scenario
and accepts running it without an OS sandbox can opt into the unrestricted
scenario profile with a separate fixed acknowledgement:

```python
from hotato.call_runtime import SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT

receipt = SippSubprocessAdapter().run(
    {
        "target": "127.0.0.1:5060",
        "scenario_path": "fixtures/reviewed-media.xml",
        "trusted_scenario_acknowledgement": SIPP_TRUSTED_SCENARIO_ACKNOWLEDGEMENT,
    },
    "artifacts/sipp-reviewed-01",
)
```

The fixed phrase is
`I_ACCEPT_SIPP_SCENARIO_HOST_COMMAND_AND_FILE_ACCESS_WITHOUT_OS_SANDBOX`.
Trusted mode still refuses DTDs, entity declarations, external entities,
processing instructions, malformed XML, excessive nesting, and excessive
element count. Destination redirection remains denied in every profile so the
scenario cannot bypass the resolved and allowlisted target. The receipt records
`trusted_host_access`, `operator_attested`, and
`os_process_sandbox: ABSENT`; it does not claim that
the scenario was confined. Run trusted scenarios only in an independently
isolated container or virtual machine with the minimum filesystem and network
access required.

RTP playback, DTMF, hold, and transfer encoded inside XML remain `UNOBSERVABLE` until target-side
events establish delivery. Warm transfer is unsupported by the generic SIPp adapter because it
does not orchestrate a verified second live leg.

## Minimum acceptance evidence

Promote a session operation from `UNOBSERVABLE` to `SUPPORTED` only after a hermetic adapter test
and a live-path fixture produce all applicable artifacts:

- normalized hash-chained events with no sequence gaps;
- target-boundary audio digest and channel/sample format;
- provider or SIP lifecycle events;
- DTMF digit plus sender/receiver evidence;
- hold start/end plus media behavior during hold;
- transfer initiation, destination-leg connection, source-leg disposition, and warm/cold mode;
- bounded stdout, stderr, network destination inventory, and process exit;
- teardown evidence for every created room, call, process, and local artifact.

Keep live-path results scoped to the provider version, region, codec, adapter commit, and fixture
that produced them. No controller capability or provider documentation establishes a universal
carrier-path result.
