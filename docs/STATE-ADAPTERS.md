# State adapters: ground a `state` assertion in your system of record

`state` and `state_change` assertions are **Authority 2**: post-call state
verification -- querying a **system of record** after the call and
comparing it against what the agent claimed ("I issued the refund"). A
state adapter is the small, pluggable seam that runs that query.

`hotato test run --state FILE` loads a state adapter from `FILE`. Three
adapters ship, and one method backs all three:

```
query(resource, **filters) -> dict | None
```

Return the record for `resource` matching every `filters` key, or `None`
when the system of record was read and holds no such record.

## The three-outcome boundary

A `state` query lands on exactly three outcomes -- the distinction is the
whole point:

- **The record is present and matches** -> `PASS`.
- **The system of record was read and holds no matching record** (or a
  field does not match) -> a grounded `FAIL`. The agent claimed something
  the record does not confirm.
- **The system of record could not be reached or read** (network error,
  timeout, a 5xx, a non-JSON response, a DB error) -> `INCONCLUSIVE`, with a
  reason every time.

A `state` assertion is satisfied by the query result alone: a lookup plus a
dict comparison, deterministic, no model in the loop.

## The three adapters

### 1. `mock`: the local sandbox (default, offline)

A JSON or SQLite fixture: `{resource: rows}`. Runs offline, byte-stable,
ready with no opt-in. Use it for regression suites and for a captured
before/after snapshot (`state_change` reads a `{"before": [...], "after":
[...]}` shape); `--state some_sandbox.json` and `--state some.sqlite3` both
select it.

### 2. `http`: a REST system of record

Queries your API over stdlib `urllib`. A **resource map** turns a query into
one request:

```json
{
  "adapter": "http",
  "egress_opt_in": true,
  "base_url": "https://api.yourcompany.com/v1",
  "auth": { "type": "bearer", "token_env": "RECORDS_API_TOKEN" },
  "timeout": 30,
  "resources": {
    "appointment": {
      "path_template": "/patients/{patient_id}/appointment",
      "method": "GET",
      "params_map": { "status": "appt_status" },
      "response_pointer": "data/appointment"
    }
  }
}
```

`query("appointment", patient_id="P1", status="booked")` fills `{patient_id}`
into the path (URL-encoded), sends the remaining filters as query params (GET)
or a JSON body (POST) under their `params_map` wire names, then extracts the
record dict at `response_pointer`.

- `response_pointer` is a JSON-pointer-ish path (`/`- or `.`-separated; digits
  index a list; empty means the whole body is the record). A pointer that
  resolves to nothing on a well-formed response means "no such record" ->
  `None`.
- `method` is `GET` (default) or `POST`.
- **HTTPS is required by default.** A plain `http://` base URL is refused
  unless you set `"allow_http": true` (only for a trusted local endpoint);
  otherwise a state query would send filter values and the auth header in
  cleartext.
- A 404 means the record does not exist -> `None` -> grounded FAIL. Every
  other non-2xx, and any network/timeout/non-JSON failure, is INCONCLUSIVE
  (`query` raises `StateAdapterError`; the assertion engine turns it into an
  INCONCLUSIVE result, with the structured cause on the adapter's
  `last_error`).

### 3. `sql`: a SQL system of record

Queries a database with a **parameterized, read-only** SELECT.

```json
{
  "adapter": "sql",
  "sqlite_path": "records.sqlite3",
  "resources": {
    "refund": {
      "query": "SELECT order_id, status, amount FROM refunds WHERE order_id = ? AND status = ?",
      "params_order": ["order_id", "status"]
    }
  }
}
```

`query("refund", order_id="O1", status="issued")` binds `[order_id, status]`
as the statement's parameters and returns the first row as a `{column:
value}` dict, or `None` when no row matches.

- Connection source (exactly one): `sqlite_path` (a local file DB, fully
  local, no egress); `dsn` + `driver` (a DBAPI module to import and
  `connect`, e.g. `psycopg2` for PostgreSQL -- a network DB needing
  `egress_opt_in: true`; imported lazily, never a hard dependency); or a
  caller-supplied `connection` object (Python API only).
- Use the placeholder style your driver expects (`?` for sqlite3, `%s` for
  psycopg2). Hotato only passes the bound value sequence through.
- A DB/driver error resolves to INCONCLUSIVE -- a query never guesses a FAIL.

## Injection safety and read-only discipline

Enforced by construction and covered in `tests/test_state_adapters_real.py`:

- **Injection-safe.** Filter values are always bound as query parameters,
  never string-interpolated into the SQL: a filter value carrying SQL
  (`"O1'; DROP TABLE refunds; --"`) is treated as a literal, matching no
  row, table untouched.
- **Read-only.** Every mapped SQL query is validated at construction (and
  re-checked before each execute): it must begin with `SELECT` (or a `WITH
  ... SELECT` CTE), carry no second statement after a `;`, and contain no
  data-modifying keyword, so a `DELETE`/`UPDATE`/`DROP`/... mapped query is
  rejected.

## Egress opt-in

The `http` adapter, and a `sql` adapter over a `dsn`, are **network paths**
(see [`docs/EGRESS.md`](EGRESS.md) and
[`docs/THREAT-MODEL.md`](THREAT-MODEL.md)). `load_state_adapter` **refuses**
them unless the config sets `"egress_opt_in": true` -- the same opt-in
`--egress-opt-in` applies to the hosted diarizer and `--notify`. Without
it, you get a clear usage error (the CLI's exit-2 path) before any
connection is made. A local `sqlite_path` SQL DB and the mock sandbox run
entirely on your machine, opening no socket, no opt-in needed.

When an `http` / non-local-`sql` query does fire, only the mapped filter
VALUES leave the machine (in the URL, query string, or JSON body). Audio,
transcript, and the config file itself stay on your machine.

## Credentials

Adapters take environment-variable **names** for credentials, keeping the
config file free of secrets and shareable. Supply the secret at run time
from the environment, e.g. `source` a `0600` file that exports it:

- `bearer`: `{ "type": "bearer", "token_env": "RECORDS_API_TOKEN" }`
- `basic`: `{ "type": "basic", "username_env": "DB_USER", "password_env": "DB_PASS" }`
  (`username` may be inline since it is not a secret).
- `header`: `{ "type": "header", "headers": { "X-Api-Key": { "env": "API_KEY" }, "X-Tenant": { "value": "acme" } } }`,
  where each header value is either `{ "env": NAME }` (a secret from the
  environment) or `{ "value": LITERAL }` (a non-secret constant).

A missing credential env var fails fast at construction, naming the
variable in the message, never the value. Credential values stay out of
logs and `last_error`.

## `state_change` and before/after snapshots

`state_change` reads a `before` and `after` snapshot to measure a delta.
Only the **mock** adapter provides both (from a captured `{"before": ...,
"after": ...}` fixture, or `<table>__before` / `<table>__after` SQLite
tables). The `http` and `sql` adapters answer point-in-time `state`
queries -- a live API/DB exposes only "now" -- so their `before` snapshot
reports absent, grounding a `state_change` against them at INCONCLUSIVE
instead of guessing "no change."

## See also

- [`docs/ASSERTIONS.md`](ASSERTIONS.md): the `state` / `state_change`
  assertion kinds.
- [`docs/EGRESS.md`](EGRESS.md), [`docs/THREAT-MODEL.md`](THREAT-MODEL.md):
  the network rows.
- `src/hotato/state_adapter.py`: the adapters; `tests/test_state_adapters_real.py`: the tests.
