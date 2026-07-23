# The team workspace: `hotato serve`

A self-hosted, local web app for a team to read a voice agent's
conversation-QA state: release readiness, the scenario matrix, a
conversation inspector, failure clusters, and production health.
Stdlib-only (`http.server` + `sqlite3`) -- no framework, no build step,
nothing that phones home. It serves the same fleet registry and evidence
store the CLI writes, reading that data directly instead of passing a
database file around.

```
hotato serve --workspace default
```

On first start it prints where it is listening, the bearer token, and the URL to
open:

```
hotato serve: workspace 'default'
  registry:  /home/you/.hotato/fleet
  listening: http://127.0.0.1:8321
  token:     Ab3xQ_p1…                 (generated, stored 0600 at …/serve/default/token)
  open:      http://127.0.0.1:8321/?token=Ab3xQ_p1…
  audit log: /home/you/.hotato/fleet/serve/default/audit.jsonl   (append-only)
  read-only: the server issues only SELECTs; reviews/labels stay CLI-driven. No telemetry, no external calls.
```

Open the `open:` URL in a browser; the server sets a session cookie and
redirects to strip the token from the address bar (see [Auth](#auth)).

## Flags

| flag | default | meaning |
|---|---|---|
| `--workspace`, `-w` | `default` | workspace id to serve |
| `--host` | `127.0.0.1` | bind address (see [Binding](#binding-127001-by-default)) |
| `--port` | `8321` | listen port |
| `--registry` | `~/.hotato/fleet` | registry home directory |
| `--production-db` | none | read session manifests and alerts from this separate Hotato production SQLite database in `/health` (mode=ro; see below) |
| `--score-production` | off | with `--production-db`: score completed sessions in the background into a `console.sqlite3` sidecar beside the evidence database (see below) |
| `--rebuild-scores` | off | with `--production-db`: deterministically regenerate the entire `console.sqlite3` sidecar from the evidence database, then exit |
| `--token` | none | supply the bearer token yourself |
| `--token-file` | none | read the bearer token from a file (first line) |

Exit codes: `0` clean shutdown (Ctrl-C); `2` usage error (unusable registry or
token, or the port was unavailable).

## The five views

Every view has a machine mirror at `?format=json` (same auth, same data) so
agents and scripts can drive the workspace without scraping HTML.

| View | URL | Shows |
|---|---|---|
| **Release readiness** | `/` | Pre-ship home screen: per-release rollup of suites/runs/evaluations -- required-suite completion, scenario/run counts, **failures by dimension** (outcome / policy / conversation / speech / reliability), inconclusive count, real-vs-simulated split, and **new-vs-fixed since the previous release**. Small samples flagged (`low sample, N=3`), never smoothed. |
| **Scenario matrix** | `/scenarios` | Rows are scenarios, columns are the current and previous release, with a per-dimension status and **reliability** (`pass^k` where a scenario has repetitions). Filterable by `agent`, `release`, `suite`, `status`. |
| **Conversation inspector** | `/conversation/<id>` | One conversation: evidence manifest, transcript, trace spans, per-dimension evaluations with rationale and citations (deterministic checks and model-judged/advisory results in **separate lanes**), reviewer decisions. Every digest links to the raw evidence (`/evidence/<digest>`); redacted transcript segments and trace spans render `[redacted]`, in both HTML and JSON. |
| **Failure clusters** | `/clusters` | Failed evaluations and assertions grouped by **observable signature** (dimension + assertion kind + reason-class), with counts and drill-through into the inspector -- it groups what was observed; the cause stays yours to determine. |
| **Production health** | `/health` | Ingest counts, evaluated coverage, and per-dimension failure rate over time, **separated for real and simulated** conversations. Sparse days/dimensions read *not enough history* rather than a misleading point. No single combined quality score -- each dimension keeps its own number. |

### Optional production-evidence bridge

The fleet registry and the production event store have different storage
authorities. Pointing the workspace at both is explicit:

```bash
hotato serve --workspace default \
  --production-db .hotato/production.sqlite3
```

`/health` then adds a separate **Production evidence plane** section with
bounded session manifests, current alerts, event-source identifiers, every
evidence lane's availability and authority, and the required lanes still
missing. The JSON mirror exposes the same projection under
`production_evidence`.

The bridge opens the selected database with SQLite `mode=ro` for each
request. It never constructs the writer-side `ProductionStore`, never selects
the event `payload_json` column, and never imports a production row into the
fleet registry. Production counts therefore stay outside `ingested_total`,
the real/simulated buckets, and release trends. The production schema does not
carry a fleet workspace id, so the UI states `workspace_scope =
not_encoded_by_production_schema` instead of silently assigning those sessions
to the workspace being served.

### Score-on-arrival (`--score-production`)

```bash
hotato serve --workspace default \
  --production-db .hotato/production.sqlite3 --score-production
```

A background worker in the same process polls the evidence database (same
`mode=ro` read-only discipline) for sessions that reached
`COMPLETE`/`QUIESCENT` and scores each one with the deterministic scorer over
the session's recorded two-channel audio (the path named by the
`media.asset.available` event's `data.path`). Bind and auth are unchanged;
the server gains no new routes or write endpoints from this flag.

Each session becomes one durable record in `console.sqlite3` beside the
evidence database:

- **`SCORED`** -- per-dimension observations (candidate counts and worst
  measured magnitude per scan kind, never blended), the ranked candidate
  moments, and one plain-English failure-reason sentence built only from
  measured numbers;
- **`NOT_SCORABLE`** -- the scorer's refusal with its reason (audio lane
  unavailable, no recorded path, a one-channel or unreadable recording);
- **`ERROR`** -- a scorer crash or persist failure on that session, with its
  reason; the worker records it and continues to the next session.

Every record carries the scorer version and a config hash, and every timing
figure derives from evidence event timestamps: per-hop latency rows keep the
reporting event's declared `authority`, turn spans and the end-to-end figure
are labeled `derived:event_timestamps`, and reported turn fields
(`yield_latency_ms`, `overlap_ms`, `duration_ms`) stay in a separate
`reported` block. Sessions are scored one at a time and a record is claimed
only after its sidecar write commits.

The sidecar is derived data -- the evidence database stays the only
authority. `--rebuild-scores` regenerates the whole sidecar from the evidence
database and exits; the same evidence database always rebuilds to identical
content (the one wall-clock column, `created_at`, is excluded from the
canonical comparison). A sidecar written by a different schema version is
refused with that rebuild instruction.

## Auth

Every request is authenticated against one shared **bearer token**:

- **Browser:** open `/?token=<token>` once; the server mints an in-memory,
  HttpOnly session cookie and redirects to remove the token from the URL.
- **Agent / API / `curl`:** send `Authorization: Bearer <token>`.

The token is compared in constant time (`hmac.compare_digest`). Without
`--token`/`--token-file`, one is generated with `secrets.token_urlsafe` on
first start and stored `0600` at `<registry>/serve/<workspace>/token`, so
a restart keeps the same URL. Sessions live only in memory, never
persisted, never cross-tenant.

## Audit log

Every request appends one JSONL line to
`<registry>/serve/<workspace>/audit.jsonl` (created `0600`):

```json
{"ts":"2026-07-12T18:04:11Z","who":"Ab3xQ_p1…","method":"GET","path":"/scenarios","query":"status=FAIL","status":200,"remote":"127.0.0.1"}
```

`who` is a token/session **prefix**, never the secret; the `token`
parameter is stripped from the recorded query. The audit log is the
**only** file the server writes.

## Binding 127.0.0.1 by default

The server binds loopback (`127.0.0.1`) unless you pass `--host`. A
non-loopback bind (e.g. `--host 0.0.0.0`, to reach the workspace from
another machine) prints a prominent warning -- it exposes the workspace to
your local network. Token auth still applies; an SSH tunnel or a reverse
proxy you control is the tighter choice over binding a wide interface.

## Zero egress

The server only opens a **listening** socket: no outbound connection, and
nothing it imports phones home -- audio, traces, and evaluations stay on
the machine. A test whitelists loopback and fails if any view attempts an
external connection, backed by a threat-model row.
