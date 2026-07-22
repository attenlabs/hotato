# Self-hosted hotato control plane

This deployment composes Hotato with LiveKit Server, LiveKit SIP, Valkey, and
an OpenTelemetry Collector. Hotato owns caller programs, load schedules,
production evidence, regression promotion, and offline verification. The
Collector accepts standard OTLP from local agents and converts traces to the
bounded JSON form Hotato persists. LiveKit owns WebRTC/SIP transport. PSTN
routes still require a SIP trunking provider; the bundle does not present
itself as a telephone carrier.

## Bring-up

Linux is required for the host-network SIP/media layout.

```bash
cd deploy/control-plane
cp .env.example .env
# replace credentials, addresses, and image refs
chmod 600 .env
python3 bootstrap.py --env .env --runtime runtime --allow-tags
docker compose --env-file .env up -d --build
```

`--allow-tags` is for a local evaluation. Omit it for a deployment; bootstrap
then requires each external service image as `name@sha256:<digest>`. The Hotato
service is built from the current checkout. Record the source commit and the
result of `docker image inspect hotato-control-plane:local --format '{{.Id}}'`
with the deployment evidence; a local tag alone is not immutable provenance.

The production evidence gateway listens on `127.0.0.1:8432`. LiveKit binds
`7880` on the host so its RTC listeners can use the host interfaces; keep 7880
blocked at the external firewall and route only a TLS-terminating reverse proxy
with a trusted certificate to `127.0.0.1:7880`. SIP and media use host networking
because the SIP service's UDP range is not suited to bridge-port enumeration.
Keep every port listed under `must_remain_firewalled` private. Open only the ports in
`runtime/bootstrap-manifest.json`, restrict sources at the firewall, and set
the advertised media address correctly. LiveKit SIP discovers its external
address with `use_external_ip`; confirm the advertised SDP address during the
transport acceptance run.

Bootstrap writes the Valkey password into the private LiveKit, SIP, and Valkey
configs and a separate password file for the health check. A short-lived init
container copies each host-private file into a service-specific named volume,
so non-root service users can read their own config without making the source
files world-readable or exposing one service's secrets to another. The production
gateway reads its bearer token from that private volume through
`hotato production serve --token-file`. The same volume carries a non-secret
maintenance policy rendered from the declared interval, quiescence, evidence
lanes, and retention values. `production serve --maintenance-policy` performs
continuous finalization, persisted alert evaluation, and retention. Neither secret value is placed in
Compose interpolation, the container environment, or process arguments.

The Collector's rendered config contains its bearer credential for the Hotato
gateway. The init container installs that config as mode `0400`, owned by the
Collector's fixed non-root UID, in an OTel-only volume. The disk queue has a
separate mode `0700` volume. The Collector container has a read-only root
filesystem, no Linux capabilities, and no secret in its environment or argv.

The example chooses a 30-day local retention window. Set
`HOTATO_RETENTION_DAYS=none` before bootstrap to disable automatic deletion.
Changing local retention cannot delete copies already sent to a collector,
backup, recording store, or provider.

After rotating any secret or config, rerender and reseed the named volumes
before restarting services:

```bash
python3 bootstrap.py --env .env --runtime runtime
docker compose --env-file .env run --rm hotato-config-init
docker compose --env-file .env restart valkey livekit livekit-sip hotato-production otel-collector
```

## OTLP ingress and bounded buffering

Configure local agents and sidecars to send standard OTLP to the Collector,
without a Hotato credential:

```text
OTLP/gRPC:          127.0.0.1:4317
OTLP/HTTP:   http://127.0.0.1:4318
```

For example, an SDK using OTLP/HTTP can set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` and
`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. OTLP/gRPC and OTLP/HTTP inputs are
bound to loopback. The Collector exports trace batches as uncompressed
OTLP/HTTP JSON to `127.0.0.1:8432/v1/traces`, adding the gateway credential and
the fixed `otel-collector` source at that private hop. Hotato then validates the
complete batch and commits it to SQLite before its gateway acknowledgment.

The OTel queue is deliberately inspectable rather than described as lossless:

- `file_storage` uses an fsyncing, single-host bbolt file in `otel-wal`;
- capacity is 10,000 **export requests**, not calls, spans, or bytes;
- an enqueued request retries without an elapsed-time limit and resumes after a
  Collector restart;
- storage compacts on start and after an allocated-size rebound (marked above
  100 MiB and triggered after draining below 10 MiB); keep headroom for the
  temporary compaction database;
- `block_on_overflow` is disabled, so a full queue, disk exhaustion, or storage
  I/O error can reject and drop new telemetry before exporter retry applies;
- an SDK acknowledgment from the Collector is not Hotato's destination commit
  receipt and does not establish end-to-end durability.

Scrape `http://127.0.0.1:8888/metrics` from the host. Alert on queue size versus
capacity, `otelcol_exporter_enqueue_failed_spans`,
`otelcol_exporter_send_failed_spans`, receiver-refused spans, and sustained
accepted-versus-sent divergence. Keep Collector logs and disk-free-space
monitoring beside those metrics. The queue is a restart buffer, not an archive
or replicated log. Deploy redundant collectors or a replicated message system
when host or disk loss is in scope.

The lower-level gateway still accepts authenticated OTLP/HTTP JSON directly at
`http://127.0.0.1:8432/v1/traces`; after commit it returns the standard empty
OTLP JSON response (`{}`). A producer that needs Hotato's richer per-event
durability receipt can use the compatibility path `/v1/otlp/traces`. Direct
producers must already emit the exact JSON contract and carry the gateway
credential.

## Transport acceptance

Before a capability is credited, execute and retain:

- inbound and outbound SIP calls;
- DTMF send/receive;
- codec negotiation and participant-separated delivered-media hashes;
- hold, cold transfer, warm transfer, disconnect, and silent-audio recovery;
- NAT/firewall restart and Redis/LiveKit/SIP recovery;
- two declared regions if the deployment claims regional coverage.

`hotato.call_runtime` keeps each capability `UNOBSERVABLE` until the configured
sidecar emits the required evidence. Provider completion never becomes a
conversation-quality pass.

## Operational flow

1. `hotato caller run` executes one bounded caller graph through the selected
   sidecar or direct LiveKit transport.
2. `hotato load caller run` schedules closed or open caller-program workloads
   and preserves one child package per started call. `hotato load telephony
   run` schedules provider-lifecycle workloads without claiming media delivery.
3. Agents export CloudEvents or OTLP/HTTP JSON to `hotato production serve`.
4. Finalization preserves missing, unavailable, unsupported, and conflicted
   evidence separately.
5. `hotato production export-regression` produces an offline-verifiable
   candidate from a production failure.
6. The candidate is promoted into the existing evidence/chaos/CI path after a
   human confirms its share and retention policy.

The control plane supplies self-hosted QA orchestration, load scheduling,
production evidence assembly, and caller-generation primitives. Whether it
can replace a deployed system is established only by the external acceptance
gates below. It composes media servers and carriers because rebuilding RTP,
WebRTC congestion control, NAT traversal, and telephone routing inside Hotato
would weaken the system.

## External acceptance gates

This repository can validate rendered configuration and the Hotato evidence
plane without Docker. The local example pins Collector Contrib `0.153.0`; a
deployment must replace that tag with the exact tested image digest. A deploy is
not production-qualified until all image digests pass `docker compose config`,
the Collector accepts both OTLP transports, its persistent queue survives a
forced restart, an induced gateway outage drains after recovery, and queue-full
and disk-full drills produce the documented counters. Also require container
health checks, an authenticated Valkey restart/recovery drill, TLS validation,
SIP/RTP firewall tests (including proof that 4317, 4318, 6379, 7880, 8090, 8091,
8432, and 8888 are unreachable externally), recorded Hotato source/image
identity, and the transport acceptance matrix above on the target host. A SIP
trunk and public DNS/certificates remain operator-supplied dependencies.
