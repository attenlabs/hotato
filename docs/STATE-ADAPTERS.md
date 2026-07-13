# State adapters: grounding a `state` assertion in your system of record

The `state` and `state_change` assertion kinds are **Authority 2** (post-call
state verification). They never trust what the agent *said* ("I issued the
refund"); they query a **system of record** after the call and compare the
actual state. A state adapter is the small, pluggable seam that does the query.

`hotato test run --state FILE` loads a state adapter from `FILE`. Three adapters
ship, and the same one method backs all of them:

```
query(resource, **filters) -> dict | None
```

Return the record for `resource` matching every `filters` key, or `None` when
the system of record can be read and holds no such record.

## The honesty boundary (why this exists)

A `state` query verifies post-call system state. It has three outcomes, and the
distinction is the whole point:

- **The record is present and matches** -> `PASS`.
- **The system of record was read and holds no such record** (or a
  field does not match) -> a grounded `FAIL`. The agent claimed something the
  record does not confirm.
- **The system of record could not be reached or read** (network error,
  timeout, a 5xx, a non-JSON response, a DB error) -> `INCONCLUSIVE`, with a
  reason. Hotato never guesses a verdict from a state it could not observe.

An LLM verdict can never satisfy a `state` assertion; there is no model path in
a state adapter. The query is a lookup plus a dict comparison, deterministic.

## The three adapters

### 1. `mock`: the local sandbox (default, offline)

A JSON or SQLite fixture: `{resource: rows}`. No network, byte-stable, no
opt-in. Use it for regression suites and for a captured before/after snapshot
(`state_change` reads a `{"before": [...], "after": [...]}` shape). This is the
adapter `--state some_sandbox.json` and `--state some.sqlite3` select today.

### 2. `http`: a REST system of record

Queries your API over stdlib `urllib`. A **resource map** turns a query into one
request:

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
into the path (URL-encoded), sends the remaining filters as query params (GET) or
a JSON body (POST) under their `params_map` wire names, then extracts the record
dict at `response_pointer` from the JSON response.

- `response_pointer` is a JSON-pointer-ish path (`/`- or `.`-separated; digits
  index a list; empty means the whole body is the record). A pointer that
  resolves to nothing on a well-formed response means "no such record" -> `None`.
- `method` is `GET` (default) or `POST`.
- **HTTPS is required by default.** A plain `http://` base URL is refused unless
  you set `"allow_http": true` (only for a trusted local endpoint); a state
  query would otherwise send filter values and the auth header in cleartext.
- A 404 means the addressed record does not exist -> `None` -> grounded FAIL.
  Every other non-2xx, and any network/timeout/non-JSON failure, is
  INCONCLUSIVE (`query` raises `StateAdapterError`, which the assertion engine
  turns into an INCONCLUSIVE result; the structured cause is on the adapter's
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

`query("refund", order_id="O1", status="issued")` binds `[order_id, status]` as
the statement's parameters and returns the first row as a `{column: value}` dict,
or `None` when no row matches.

- Connection source (exactly one): `sqlite_path` (a local file DB, fully local,
  no egress), a `dsn` + `driver` (a DBAPI module to import and `connect`, e.g.
  `psycopg2` for PostgreSQL, a network DB, so it needs `egress_opt_in: true`;
  the driver is imported lazily and is never a hard dependency of hotato), or a
  caller-supplied `connection` object (Python API only).
- Use the placeholder style your driver expects (`?` for sqlite3, `%s` for
  psycopg2). Hotato only passes the bound value sequence through.
- A DB/driver error is INCONCLUSIVE (never a guessed FAIL).

## Injection safety and read-only discipline (enforced + tested)

- **Injection-safe by construction.** Filter values are ALWAYS bound as query
  parameters; they are never string-interpolated into the SQL. A filter value
  carrying SQL (`"O1'; DROP TABLE refunds; --"`) is treated as a literal: it
  matches no row and the table is untouched. Tested in
  `tests/test_state_adapters_real.py`.
- **Read-only.** Every mapped SQL query is validated at construction (and
  re-checked before each execute): it must begin with `SELECT` (or a
  `WITH ... SELECT` CTE), carry no second statement after a `;`, and contain no
  data-modifying keyword. A `DELETE`/`UPDATE`/`DROP`/… mapped query is rejected.

## Egress opt-in (the network adapters are refused without it)

The `http` adapter, and a `sql` adapter over a `dsn`, are **network paths** (see
[`docs/EGRESS.md`](EGRESS.md) and [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)).
`load_state_adapter` **refuses** them unless the config sets
`"egress_opt_in": true`, an explicit, per-config opt-in, the same discipline
`--egress-opt-in` applies to the hosted diarizer and `--notify`. Without it, you
get a clear usage error (the CLI's exit-2 path) before any connection is made.
A local `sqlite_path` SQL DB and the mock sandbox open no socket and need no
opt-in.

When an `http` / non-local-`sql` query does fire, only the mapped filter VALUES
leave the machine (in the URL, query string, or JSON body). Audio, transcript,
and the config file itself never leave.

## Credentials

Adapters take environment-variable **names**, never inline secrets, so a config
file can be committed or shared without a credential in it. Supply the secret at
run time from the environment (for example, `source` a `0600` file that exports
it):

- `bearer`: `{ "type": "bearer", "token_env": "RECORDS_API_TOKEN" }`
- `basic`: `{ "type": "basic", "username_env": "DB_USER", "password_env": "DB_PASS" }`
  (`username` may be inline since it is not a secret).
- `header`: `{ "type": "header", "headers": { "X-Api-Key": { "env": "API_KEY" }, "X-Tenant": { "value": "acme" } } }`
  where each header value is either `{ "env": NAME }` (a secret from the environment)
  or `{ "value": LITERAL }` (a non-secret constant).

A missing credential env var fails fast at construction with a message naming the
variable (never the value). Credential values are never logged and never placed
in `last_error`.

## `state_change` and before/after snapshots

`state_change` reads a `before` and an `after` snapshot to measure a delta. Only
the **mock** adapter provides both (from a captured `{"before": ..., "after":
...}` fixture, or `<table>__before` / `<table>__after` SQLite tables). The `http`
and `sql` adapters answer point-in-time `state` queries (a live API/DB exposes
only "now"), so their `before` snapshot is reported absent, and a `state_change`
against them is INCONCLUSIVE rather than a fabricated "no change".

## See also

- [`docs/ASSERTIONS.md`](ASSERTIONS.md): the `state` / `state_change` assertion kinds.
- [`docs/EGRESS.md`](EGRESS.md), [`docs/THREAT-MODEL.md`](THREAT-MODEL.md): the network rows.
- `src/hotato/state_adapter.py`: the adapters; `tests/test_state_adapters_real.py`: the tests.
