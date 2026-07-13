# The team workspace — `hotato serve`

A self-hosted, local web app for a team to read a voice agent's conversation-QA
state: release readiness, the scenario matrix, a conversation inspector, failure
clusters, and production health. It is stdlib-only (`http.server` + `sqlite3`) —
no framework, no build step, no external services, no telemetry. It serves the
same fleet registry and evidence store the CLI writes; it never passes a database
file around.

```
hotato serve --workspace default
```

On first start it prints where it is listening, the bearer token, and the URL to
open:

```
hotato serve — workspace 'default'
  registry:  /home/you/.hotato/fleet
  listening: http://127.0.0.1:8321
  token:     Ab3xQ_p1…                 (generated, stored 0600 at …/serve/default/token)
  open:      http://127.0.0.1:8321/?token=Ab3xQ_p1…
  audit log: /home/you/.hotato/fleet/serve/default/audit.jsonl   (append-only)
  read-only: the server issues only SELECTs; reviews/labels stay CLI-driven. No telemetry, no external calls.
```

Open the `open:` URL in a browser. The server sets an HttpOnly session cookie and
redirects to strip the token from the address bar; from then on you navigate
without the secret in the URL.

## Flags

| flag | default | meaning |
|---|---|---|
| `--workspace`, `-w` | `default` | workspace id to serve |
| `--host` | `127.0.0.1` | bind address (see [Binding](#binding-127001-by-default)) |
| `--port` | `8321` | listen port |
| `--registry` | `~/.hotato/fleet` | registry home directory |
| `--token` | — | supply the bearer token yourself |
| `--token-file` | — | read the bearer token from a file (first line) |

Exit codes: `0` clean shutdown (Ctrl-C); `2` usage error (unusable registry or
token, or the port was unavailable).

## The five views

Every view has a machine mirror at `?format=json` (same auth, same data) so
agents and scripts can drive the workspace without scraping HTML.

1. **Release readiness** (`/`) — the pre-ship home screen. Per-release rollup
   from `suites`/`runs`/`evaluations`: whether required suites are complete,
   scenario and run counts, **failures by dimension** (outcome / policy /
   conversation / speech / reliability), the inconclusive count, the origin split
   (real vs simulated, kept separate), and **new-vs-fixed since the previous
   release** compared per (scenario, dimension). Small samples are flagged
   (`low sample, N=3`), never smoothed.
2. **Scenario matrix** (`/scenarios`) — rows are scenarios, columns are the
   current and previous release, with a per-dimension status and **reliability**
   (`pass^k` where a scenario has repetitions). Filter by `agent`, `release`,
   `suite`, and `status` via query parameters (there is a filter form at the top).
3. **Conversation inspector** (`/conversation/<id>`) — one conversation: its
   evidence manifest (`conversation.v1` — origin, provider/caller provenance,
   child digests), transcript, trace spans, per-dimension evaluations with
   rationale and citations (deterministic checks and model-judged/advisory
   results shown in **separate lanes**), and reviewer decisions. Every digest is
   a link to the raw evidence blob (`/evidence/<digest>`) — drill straight to the
   source. Redacted transcript segments and trace spans render as `[redacted]`
   and the redacted text is scrubbed from both the HTML and the JSON mirror.
4. **Failure clusters** (`/clusters`) — failed evaluations and assertions grouped
   by **observable signature** (dimension + assertion kind + reason-class), with
   counts and drill-through lists into the inspector. This is labelled *clusters
   by observable signature* — it groups what was observed and does not claim a
   cause.
5. **Production health** (`/health`) — ingest counts, evaluated coverage, and
   per-dimension failure rate over time, computed **separately for real and
   simulated** conversations (never merged). Days with no evaluated sample get no
   point, and a dimension with fewer than two days of data reads *not enough
   history* — the same honesty the trend report uses. There is no single combined
   quality number anywhere.

## Auth

Every request is authenticated against one shared **bearer token**:

- **Browser:** open `/?token=<token>` once; the server mints an in-memory,
  HttpOnly session cookie and redirects to remove the token from the URL.
- **Agent / API / `curl`:** send `Authorization: Bearer <token>`.

The token is compared in constant time (`hmac.compare_digest`). If you do not
pass `--token`/`--token-file`, one is generated with `secrets.token_urlsafe` on
first start and stored `0600` at `<registry>/serve/<workspace>/token`, so a
restart keeps the same URL. Sessions live only in memory (never persisted, never
cross-tenant).

## Audit log

Every request appends one JSONL line to
`<registry>/serve/<workspace>/audit.jsonl` (created `0600`):

```json
{"ts":"2026-07-12T18:04:11Z","who":"Ab3xQ_p1…","method":"GET","path":"/scenarios","query":"status=FAIL","status":200,"remote":"127.0.0.1"}
```

`who` is a token/session **prefix**, never the secret; the `token` parameter is
stripped from the recorded query. The audit log is the **only** file the server
writes.

## Read-only in v1

The server mutates nothing except the audit log. It issues only `SELECT`s against
your workspace and never exposes a write endpoint. Reviews, labels, and
adjudications stay CLI-driven (`hotato fleet review …`, `hotato label …`); the UI
states this in its footer.

## Binding 127.0.0.1 by default

The server binds loopback (`127.0.0.1`) unless you explicitly pass `--host`. A
non-loopback bind (for example `--host 0.0.0.0`, to reach the workspace from
another machine) prints a prominent warning, because it exposes the workspace to
your local network. Token auth still applies, but prefer an SSH tunnel or a
reverse proxy you control over binding a wide interface directly.

## Zero egress

The server only opens a **listening** socket. It never makes an outbound
connection and imports nothing that phones home; audio, traces, and evaluations
stay on the machine. This is covered by a test that whitelists loopback and fails
if any view attempts an external connection, and by the threat-model row below.
