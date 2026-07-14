# Self-host hotato in your own cloud / VPC

Run the complete conversation-QA team workspace on infrastructure you
control. Call audio, transcripts, traces, and evaluations stay on your
machine; the default stack binds a local port and stops there. This page
walks the whole path: build, bring it up, connect your own calls, add a
local model judge, back it up, and upgrade.

The stack is small on purpose:

- **hotato**: the `pip`-installable package. The core is stdlib-only (zero
  runtime dependencies), so the default image stays offline at run time, with
  zero added supply-chain surface.
- **`hotato serve`**: the read-only, token-authenticated team workspace (five
  views: release readiness, scenario matrix, conversation inspector, failure
  clusters, production health). See [`docs/WORKSPACE.md`](WORKSPACE.md).
- **Ollama** (optional): a local model judge for the rubric lane, opt-in
  behind a compose profile. The default path stays local end to end.

---

## Prerequisites

- Docker Engine 24+ and the Docker Compose v2 plugin, v2.24+ (`docker compose`,
  not the legacy `docker-compose`; the optional `env_file` uses the long-form
  `required:` field added in v2.24). Check with `docker compose version`.
- ~1 GB of disk for the default image; a persistent volume for `/data`.
- Docker alone builds and runs the default stack -- self-hosted, offline,
  nothing else to provide. (The optional judge model download is the one
  documented exception; see [Enable the local model judge](#enable-the-local-model-judge-optional).)

The files that make up the deployment:

| File | What it is |
|---|---|
| `Dockerfile` | Multi-stage, slim, non-root image that installs hotato from source |
| `docker-compose.yml` | The workspace service, an optional judge, and a one-shot demo seeder |
| `deploy/entrypoint.sh` | Injects the bearer token from a secret/env, then starts the server |
| `deploy/healthcheck.py` | The container HEALTHCHECK (authenticated GET over loopback) |
| `deploy/seed-demo.py` | Seeds a small, clearly-labelled example dataset (optional) |
| `deploy/verify-zero-egress.sh` | Proves the default stack makes no external calls (see below) |
| `deploy/hotato.env.example` | Optional environment (token, judge model name) |

---

## Build

From the repository root:

```bash
docker compose build
```

This builds `hotato-selfhost:local` from the local source. Two extras build
in with build args when you need them inside the workspace container:

```bash
# add a local faster-whisper ASR pass (offline once a model is cached):
docker compose build --build-arg WITH_TRANSCRIBE=1
# add the Ed25519 evidence-signing layer (cryptography):
docker compose build --build-arg WITH_SIGN=1
```

The default build installs the stdlib-only core, which keeps the image
small and offline. The heavier live-capture and diarization extras run
inside your own voice pipeline instead, kept out of this container by
design.

---

## Bring it up

```bash
docker compose up -d
```

The workspace publishes to host loopback only:

```
http://127.0.0.1:8321
```

Only that one port is published, and only on `127.0.0.1`. Inside the
container the server binds `0.0.0.0:8321`, the interface a published port
requires; it prints a non-loopback-bind warning at start as an expected
side effect of that binding. The compose port mapping
(`127.0.0.1:8321:8321`) is what keeps the workspace on the host's loopback
only. To reach it from your laptop, use an SSH tunnel or a reverse proxy
you control, and keep the published binding as-is.

Open the workspace with the bearer token (see
[Credentials](#credentials-and-the-bearer-token)):

```bash
# print the token the server generated on first start
docker compose exec hotato-workspace cat /data/serve/default/token
# then open http://127.0.0.1:8321/?token=<token> once in a browser
```

Health: the container ships a HEALTHCHECK that authenticates to the server
over loopback and checks a view returns `200`. `docker compose ps` shows
`healthy` once it is serving.

---

## First boot: example data

A brand-new workspace is empty. To populate the five views immediately with
a small, clearly-labelled **example** dataset (two releases, three
scenarios, a mix of pass / fail / inconclusive across the five dimensions,
and both direct and simulated origins), run the one-shot seeder:

```bash
docker compose --profile demo run --rm hotato-init
```

The seeder lives in the `demo` compose profile, so a plain
`docker compose up -d` leaves it out entirely. `docker compose run` targets
that profiled service directly; the `--profile demo` above makes the
profile explicit.

> `hotato start --demo` writes a *sweep report* (`hotato-sweep.json` + an HTML
> dashboard) into a directory -- a separate thing from populating the workspace,
> which reads the fleet registry's entity model. The seeder writes that entity
> model through the same public API the CLI uses.

This example data is labeled and scoped to itself: it says nothing about
any agent you run. `origin` is set per conversation (direct vs simulated),
keeping the two in separate buckets.

To keep your own calls clear of the example rows, ingest into a **different
workspace id** (the demo lives in `default`), or reset the volume before you
start:

```bash
# your own data in its own workspace, leaving the demo in `default`:
docker compose exec hotato-workspace hotato fleet ingest --home /data -w acme ...
# or wipe everything (demo + all data) and start clean:
docker compose down -v
```

`python3 /opt/hotato-deploy/seed-demo.py --clear` prints the reset guidance;
the registry resets at the volume level, so a volume reset is the reliable
way to remove the example data.

---

## Connect your own data

The workspace reads the registry + evidence store under the `/data` volume.
Your CLI runs inside the same container, against the same `/data`, so
everything the workspace shows comes from calls you ingest.

Score and register a two-channel recording (caller on channel 0, agent on
channel 1). Mount the folder that holds your recordings, then run the CLI
in the container:

```bash
# make your recordings available at /calls inside the container
docker compose run --rm -v /path/to/your/recordings:/calls:ro \
  hotato-workspace hotato fleet ingest --home /data -w default \
  --agent support-bot /calls/one-call.wav
```

Open the workspace next: the conversation, its evidence, and any
evaluations appear in the inspector and the health view. The full lifecycle
-- register an agent, run a suite, compare releases, review failures --
uses the same `docker compose exec hotato-workspace hotato …` pattern:

```bash
docker compose exec hotato-workspace hotato fleet agent add \
  --home /data -w default --agent-id support-bot --stack vapi
docker compose exec hotato-workspace hotato fleet status --home /data -w default
```

`--home /data` points the CLI at the same registry the server serves with
`--registry /data`. Reviews and labels stay CLI-driven; the workspace
stays read-only, writing only to its own audit log.

Fetching calls from a hosted voice provider (`hotato pull` / `capture`)
reaches that provider's API with credentials you supply -- an opt-in path,
separate from the default stack, listed in [`docs/EGRESS.md`](EGRESS.md).

---

## Enable the local model judge (optional)

The rubric lane scores a transcript against criteria with a local model.
The default judge is an Ollama daemon; enable it with the `judge` profile:

```bash
docker compose --profile judge up -d
```

The Ollama service stays off any published port: it is reachable only on
the private compose network, wired to the workspace via
`HOTATO_JUDGE_ENDPOINT=http://ollama:11434`. Pull the pinned judge model
once (`qwen2.5vl:3b` is the model `hotato rubric run` uses by default, so
no `--judge-model` flag is needed later):

```bash
docker compose exec ollama ollama pull qwen2.5vl:3b
```

> **This pull downloads model weights from the internet**: the one documented
> download, like installing any package that carries model weights. It happens
> once, into the `ollama-models` volume; after that, inference runs offline.

Run the rubric lane inside the container, pointing it at a rubrics file and
a transcript you supply (author a rubric per [`docs/RUBRIC.md`](RUBRIC.md),
then mount or copy both into the `/data` volume). The judge endpoint
hostname (`ollama`, wired by `HOTATO_JUDGE_ENDPOINT` in the compose file)
isn't loopback, so hotato's endpoint gate asks you to acknowledge it with
`--judge-egress-opt-in`. That traffic stays on the private compose network,
inside the host the whole way; the flag exists because the gate keys on the
hostname, independent of where the packet travels:

```bash
docker compose exec hotato-workspace hotato rubric run \
  --rubrics /data/rubric.json --transcript /data/transcript.json \
  --judge-egress-opt-in --judge-endpoint http://ollama:11434
```

The lane is advisory by default: a rubric FAIL is reported but the exit
code stays `0`; add `--gate` to fail CI on a FAIL. The judge uses the
pinned `qwen2.5vl:3b` model you pulled above; pass `--judge-model <id>` to
pick another. The rubric result is model-judged (`deterministic: false`)
and stays on its own lane, apart from the deterministic assertion counts.

### Pre-seed the judge model for an air-gapped deploy

On a machine with network access, pull the model into a named volume, then
move that volume (or its backing directory) to the air-gapped host:

```bash
# on a connected machine
docker volume create ollama-models
docker run --rm -v ollama-models:/root/.ollama ollama/ollama:latest \
  sh -c "ollama serve & sleep 5 && ollama pull qwen2.5vl:3b"
# back up the volume and restore it as `hotato_ollama-models` on the target
docker run --rm -v ollama-models:/data -v "$PWD":/backup alpine \
  tar czf /backup/ollama-models.tgz -C /data .
```

With the model already in the volume, the air-gapped stack runs the judge
with no download.

---

## Credentials and the bearer token

Every request to the workspace is authenticated against one shared bearer
token, compared in constant time. Three ways to provide it (precedence high
to low):

1. **A Docker secret** (best). Mount a secret file at
   `/run/secrets/hotato_token`; the entrypoint passes it as `--token-file`,
   so the token stays off the process command line. Example addition to
   `docker-compose.yml`:

   ```yaml
   services:
     hotato-workspace:
       secrets:
         - hotato_token
   secrets:
     hotato_token:
       file: ./deploy/hotato_token.txt   # chmod 0600
   ```

2. **An env var.** Set `HOTATO_SERVE_TOKEN` in `deploy/hotato.env` (copy it
   from `deploy/hotato.env.example`). The entrypoint writes it to a `0600`
   file in the container and passes `--token-file`, so it stays off the
   command line.

3. **Generated.** Set nothing and the server generates a token with
   `secrets.token_urlsafe` on first start and stores it `0600` at
   `/data/serve/default/token`. Read it with
   `docker compose exec hotato-workspace cat /data/serve/default/token`.

The token file and the audit log are written with owner-only (`0600`)
permissions. Keep `deploy/hotato.env` and any token file `0600` on the host
too, and keep them out of version control.

---

## Backup and restore

Everything the workspace needs lives in the `/data` volume: the registry
(SQLite), the content-addressed evidence store, the serve token, and the
audit log. Back up that one volume:

```bash
# back up hotato_hotato-data to ./hotato-data-backup.tgz
docker run --rm -v hotato_hotato-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/hotato-data-backup.tgz -C /data .

# restore into a fresh volume
docker volume create hotato_hotato-data
docker run --rm -v hotato_hotato-data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/hotato-data-backup.tgz"
```

Because the evidence store is content-addressed (sha256), a restored
artifact is the same bytes that produced the original verdict; any digest
mismatch is caught and surfaced explicitly.

---

## Upgrade

The image installs hotato from the source in this repository, so upgrading
is a rebuild:

```bash
git pull                       # or check out the release tag you want
docker compose build --pull    # rebuild the image
docker compose up -d           # recreate the workspace container
```

The `/data` volume is untouched by a rebuild, so your registry and evidence
survive the upgrade. The registry re-asserts its (idempotent) schema on
open. Back up `/data` before a major upgrade, as with any stateful service.

---

## Zero-migration promise

The `/data` registry and the content-addressed evidence store use the same
schemas the managed cloud uses. Your conversation artifacts, conversation
tests, and dashboards move between self-hosted and cloud without changing
a line. Self-host is the same platform on your own infrastructure, with
the whole QA platform available there -- no hosted login required.

---

## Air-gapped deployment

The default stack (the workspace alone) runs offline at run time, so it
works on an air-gapped host once the image is present. Bring the image over
instead of pulling it on the target:

```bash
# on a connected machine
docker compose build
docker save hotato-selfhost:local | gzip > hotato-selfhost.tar.gz
# move the tarball to the air-gapped host, then:
gunzip -c hotato-selfhost.tar.gz | docker load
docker compose up -d
```

This is air-gapped once its one-time steps are done: enabling the judge
profile needs the model pull described above unless you pre-seed the
`ollama-models` volume. With the image loaded and (if you use the judge)
the model volume pre-seeded, the whole stack runs fully offline.

---

## What "stays offline" covers

Scope, stated precisely so it holds up:

- The claim is about the default stack's run-time behaviour: the workspace
  server binds a listening socket and stops there -- it imports nothing
  that phones home, and keeps audio, traces, and evaluations on the
  machine. The workspace is read-only, writing only its own append-only
  audit log.
- The default workspace runs on a normal Docker bridge so its port can
  publish; the guarantee rests on the server's own behaviour, and you can
  verify it directly on your own machine, independent of any firewall:

  ```bash
  ./deploy/verify-zero-egress.sh
  ```

  It (1) confirms only `127.0.0.1:8321` is published, (2) runs the same
  image on an `internal` Docker network where egress is physically removed
  and shows the workspace still answers a view with `200` (proof the
  server does its job entirely on what's already on the machine), and (3)
  lists ESTABLISHED connections inside the running container and confirms
  every one stays internal.

- **Every opt-in path that reaches the network is named explicitly.** The
  local judge talks to the in-stack Ollama over the private network (and
  the one-time model pull downloads weights); `hotato pull` / `capture`
  fetch calls from a provider you configure; a hosted judge or the
  pyannoteAI diarizer send data off-box only behind an explicit
  `--judge-egress-opt-in` / `--egress-opt-in` flag. Every one of these is
  enumerated, command by command, in [`docs/EGRESS.md`](EGRESS.md) and
  [`docs/THREAT-MODEL.md`](THREAT-MODEL.md).

A capability that lacks its input for a given run returns INCONCLUSIVE --
plain and consistent, whether you self-host or not.

---

## See also

- [`docs/WORKSPACE.md`](WORKSPACE.md): the five views, auth, and the audit log
- [`docs/EGRESS.md`](EGRESS.md): every network call site, command by command
- [`docs/THREAT-MODEL.md`](THREAT-MODEL.md): the offline / opt-in split and the
  workspace's threat-model row
- [`SECURITY.md`](../SECURITY.md): posture summary and reporting a vulnerability
