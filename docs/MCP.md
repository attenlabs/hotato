# MCP: the hotato server

hotato ships an MCP server, `hotato-mcp`, that speaks MCP over stdio and
exposes twelve tools: one scoring tool, `voice_eval_run`, which returns the
identical JSON envelope (`schema_version` "1") the CLI emits, plus eleven fleet
tools. Eight read, verify, and propose over a local fleet workspace; three are
clone-scoped actions that recompute, never deploy. None of them deploys to
production. Everything runs locally; no audio leaves the machine.

Every tool response carries a uniform control envelope: four keys ride on every
response (pure reads included) so an autonomous caller parses one shape:
`evidence_status` (or null for a pure read with no verdict), `refusal_reason`
(or null), `artifact_digests` (a list, or `[]`), and
`pending_irreversible_action` (the exact human-gated action still pending, e.g.
deployment approval, or null).

## Run it (zero-install)

```bash
uvx --from "hotato[mcp]" hotato-mcp
```

**Common mistake:** `uvx hotato-mcp` (no `--from`) FAILS. uv then looks for a
package literally named `hotato-mcp` on PyPI, which does not exist; the
console script `hotato-mcp` lives inside the `hotato` distribution, installed
with its `mcp` extra. If you (or an agent) just tried the bare form and got a
"package not found" error, retry with `--from "hotato[mcp]"` exactly as
written above. If hotato is already installed in your environment,
`python -m hotato.mcp_server` also works and needs no extra invocation syntax.

## Add it to a client

Every block below is copy-paste exact; only the outer key (client-specific)
differs. All three use the same command and args.

### Claude Desktop

Edit `claude_desktop_config.json` (Settings -> Developer -> Edit Config):

```json
{
  "mcpServers": {
    "hotato": {
      "command": "uvx",
      "args": ["--from", "hotato[mcp]", "hotato-mcp"]
    }
  }
}
```

### Cursor

Project-scoped: `.cursor/mcp.json` in your repo root. User-scoped:
`~/.cursor/mcp.json`.

```json
{
  "mcpServers": {
    "hotato": {
      "command": "uvx",
      "args": ["--from", "hotato[mcp]", "hotato-mcp"]
    }
  }
}
```

### Codex CLI

Edit `~/.codex/config.toml`:

```toml
[mcp_servers.hotato]
command = "uvx"
args = ["--from", "hotato[mcp]", "hotato-mcp"]
```

## The scoring tool: `voice_eval_run`

Same two input modes as the CLI, all parameters optional beyond the mode you
pick:

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `stereo` | str | None | two-channel WAV path |
| `caller` | str | None | mono caller WAV (with `agent`) |
| `agent` | str | None | mono agent WAV (with `caller`) |
| `suite` | str | None | `"barge-in"` to run the bundled battery |
| `stack` | str | `"generic"` | livekit, pipecat, vapi, or generic |
| `expect` | str | `"yield"` | `"yield"` or `"hold"` |
| `onset_sec` | float | None | caller onset hint |
| `caller_channel` | int | 0 | caller channel index |
| `agent_channel` | int | 1 | agent channel index |
| `max_talk_over_sec` | float | None | pass threshold |
| `max_time_to_yield_sec` | float | None | pass threshold |
| `report_path` | str | None | also write the HTML report here; the envelope then carries `report_path` (absolute) |

Pass exactly one input mode: `stereo`, OR `caller` + `agent` together, OR
`suite`. Mixing modes (or passing none) is a structured error, never a raw
exception; see below.

## Output

On success, the identical envelope the CLI emits for `--format json`, schema
at [`https://hotato.dev/schema/envelope.v1.json`](https://hotato.dev/schema/envelope.v1.json)
(`schema_version` "1", additive-only: new keys may appear, the documented
core never changes).

Every expected failure (a missing / mono / mismatched / not-found file, an
unknown suite, an ambiguous input mode, or a well-formed input with no
scorable event) comes back as a structured error object, `ok: false` with a
stable `error_code` and an actionable `message`, schema at
[`https://hotato.dev/schema/error.v1.json`](https://hotato.dev/schema/error.v1.json)
-- never a raw uncaught exception. The MCP tool and the CLI share this one
error shape, so a caller (human or agent) parses one contract across both
surfaces.

## The fleet tools

Eleven tools drive the local, self-hosted fleet control plane
([`GUARDIAN-FLEET.md`](GUARDIAN-FLEET.md)). They read and reason over a
workspace; they never auto-label and never deploy to production.

| Tool | Scope | Does |
| --- | --- | --- |
| `fleet_status` | read-only | workspace counts + job-queue stats |
| `candidate_list` | read-only | top candidate moments awaiting a human label |
| `candidate_inspect` | read-only | one candidate's detail: onset, measured components (severity/input_health/recurrence/novelty/covered_by_contract), and trust findings |
| `contract_list` | read-only | contracts in a workspace |
| `trial_explain` | read-only | a recorded trial's verdict, evidence tier, and any pending human-gated action |
| `experiment_status` | read-only | a trial's current verdict, evidence tier, recommendation, and manifest hash |
| `artifact_verify` | read-only | a contract bundle's authenticity + evidence, without trusting it |
| `experiment_propose` | read-only | a bounded variant set with expected effects; does not clone, apply, or deploy |
| `experiment_create` | clone-scoped | precommit a trial manifest from a committed battery BEFORE any capture; never captures, never deploys |
| `experiment_run` | clone-scoped | recompute a before/after trial offline (no network, no production mutation) and record a recommendation |
| `clone_cleanup` | clone-scoped | delete a STAGING clone an experiment created; never touches production |

## More

- Python API and the shared error contract: [`API.md`](API.md)
- What the envelope measures and the scope/ceiling: the top-level
  [`README.md`](../README.md) and [`METHODOLOGY.md`](../METHODOLOGY.md)
- Machine-readable index of every command (CLI and MCP): [`llms.txt`](../llms.txt)
