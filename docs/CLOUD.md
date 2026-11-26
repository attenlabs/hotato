# hotato cloud: the team web app

A web app for a team: scored production calls, persona test runs, alert rules, and the
connections that feed them, in one place. The same app runs self-hosted or hosted.
It is an optional extra (`pip install 'hotato[cloud]'`); the hotato core stays
stdlib-only and never imports it.

## Self-host in one command

```bash
export HOTATO_CLOUD_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
export POSTGRES_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
docker compose -f docker-compose.cloud.yml up -d --build
```

Open `http://127.0.0.1:8080`. The first visit to `/signup` creates the first account and
its organisation; after that sign-up closes unless you set `HOTATO_CLOUD_ALLOW_SIGNUP=true`.
Everyone else joins through an invite link from Settings, or you run
`hotato cloud admin create-user someone@example.com` inside the container.

Without Docker: `pip install 'hotato[cloud]'`, set `HOTATO_CLOUD_SECRET`, run
`hotato cloud serve --port 8080` (SQLite file `hotato-cloud.sqlite3` in the working directory).

## Send data to it

1. In Settings create an API token (shown once; stored as a hash). Note the project id.
2. Push artifacts from any machine. This needs no extra install:

```bash
hotato cloud push ./out --url https://hotato.example.com --project <project-id> --token $HOTATO_TOKEN
```

`push` reads a file or a directory of `.json` / `.jsonl`, posts each `hotato.testrun.v1` to
`POST /api/v1/projects/{id}/testruns` and each `hotato.monitorevent.v1` (batched) to
`POST /api/v1/projects/{id}/events`. It refuses to send a token over plain http to a non-local
host. Re-posting the same `run_id` or `call_id` replaces the earlier record.

Validation is strict: required fields and types are enforced, an unknown `schema` string is
rejected with HTTP 422 and a path-qualified message, a batch is all-or-nothing. Extra keys are
ignored, so a newer producer does not break ingest.

The monitor fetches alert rules with `GET /api/v1/projects/{id}/rules` (bearer token). Rules
are edited under Alerts.

## Environment variables

| Variable | Meaning |
|---|---|
| `HOTATO_CLOUD_SECRET` | Required, 16+ characters. Encrypts stored API keys (Fernet) and keys CSRF tokens. The app refuses to start without it. Changing it makes saved connection keys unreadable: re-enter them. |
| `DATABASE_URL` | SQLAlchemy URL. Default `sqlite:///hotato-cloud.sqlite3`. Postgres: `postgresql://user:pw@host/db`. |
| `HOTATO_CLOUD_ALLOW_SIGNUP` | `true` lets anyone create an organisation. Default false (the first account is always allowed). |
| `HOTATO_CLOUD_SECURE_COOKIES` | Force the Secure cookie flag on or off. Default: on when the request is https. |
| `HOTATO_CLOUD_TRUST_PROXY` | `true` when behind one reverse proxy: trust `X-Forwarded-Proto` and the last `X-Forwarded-For` hop. Leave false when exposed directly. |
| `HOTATO_CLOUD_MAX_BODY_BYTES` | Ingest body limit, default 8 MiB. |

Run it behind a TLS-terminating proxy for anything but local use.

## Roles

Owners manage tokens, connections, members, invites and projects. Members read everything in
their organisation's projects and edit alert rules.

## Backup

Everything is in the database. Postgres:
`docker compose -f docker-compose.cloud.yml exec db pg_dump -U hotato hotato > hotato-backup.sql`.
SQLite: `sqlite3 hotato-cloud.sqlite3 ".backup out.sqlite3"`. Keep `HOTATO_CLOUD_SECRET` with
the backup: saved connection keys cannot be decrypted without it. Restore: load the dump into an
empty database and start the app with the same secret.

## Upgrade

```bash
git pull
docker compose -f docker-compose.cloud.yml up -d --build
```

On start the app creates any tables that are missing. It does not alter existing tables, so back
up first; a release that changes a column carries a migration note in the changelog.

## Security notes

Passwords: Argon2. Sessions: random token in an HttpOnly, SameSite=Lax cookie (Secure over
https), stored hashed, 14 days. Forms carry a CSRF token bound to the session, plus an Origin
check. Login, sign-up and invite attempts are rate limited per address and per email. API tokens
and invite links are stored as SHA-256 hashes. Every project query is joined to the caller's
membership; a project that is not yours answers 404. Pages send a strict Content-Security-Policy
(no inline script) and escape all stored text.
