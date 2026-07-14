# MCP: the hotato server

hotato ships an MCP server, `hotato-mcp`, over stdio, with fifteen tools:
one scoring tool, `voice_eval_run` (returns the identical JSON envelope,
`schema_version` "1", the CLI emits); three proof-preserving counterexample
tools that compile and check offline regression capsules; and eleven fleet
tools -- eight read/verify/propose over a local fleet workspace, three
clone-scoped actions that recompute and hand the deploy decision back to
you. Everything, including audio, runs and stays on your machine.

Every tool response carries a uniform control envelope: four keys, pure
reads included, so an autonomous caller parses one shape: `evidence_status`
(or null for a pure read with no verdict), `refusal_reason` (or null),
`artifact_digests` (a list, or `[]`), and `pending_irreversible_action`
(the exact human-gated action still pending, e.g. deployment approval, or
null).

## Run it (zero-install)

```bash
uvx --from "hotato[mcp]" hotato-mcp
```

**Common mistake:** `uvx hotato-mcp` (no `--from`) FAILS -- uv looks for a
package literally named `hotato-mcp` on PyPI, which does not exist; the
console script lives inside the `hotato` distribution's `mcp` extra. Hit
that "package not found" error? Retry with `--from "hotato[mcp]"` exactly
as above. Already have hotato installed? `python -m hotato.mcp_server`
works too, no extra syntax needed.

## Add it to a client

Every block below is copy-paste exact; only the outer key (client-specific)
differs -- all three use the same command and args.

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

Same two input modes as the CLI, all parameters optional beyond your chosen
mode:

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
`suite`. Mixing modes (or passing none) returns a structured, parseable
error with a stable `error_code` and message; see below.

## The counterexample tools

These three tools call the same stdlib-only core as
`hotato counterexample ...`. `HOTATO_MCP_INPUT_DIR` confines scenario, test,
and capsule reads; `HOTATO_MCP_REPORT_DIR` confines new capsule writes; with
neither set, reads and writes stay under the working directory or the OS
temp directory. Compilation never overwrites an existing path.

| Tool | Scope | Does |
| --- | --- | --- |
| `counterexample_compile` | local write | reduces one failing scripted `hotato.scenario` + conversation-test target into a new private `.hotato-repro` capsule |
| `counterexample_verify` | read-only | audits source bytes, evaluator provenance, the replayed delete-only chain, the source-selected structured failure branch, and a completed local-minimality claim |
| `counterexample_reproduce` | read-only | runs only the reduced fixture under the current evaluator, permitting evaluator-version drift while still requiring the source-selected structured failure branch |

`counterexample_compile` takes `scenario_path`, `test_path`, `target`,
`out_dir`, optional `budget` (candidate evaluations), and optional `seed`.
The compiler selects the base scenario at that seed and does not expand
`variation_matrix`. `counterexample_verify` and `counterexample_reproduce`
each take one `path`.

The evidence status is `asserted`: this path operates on a deterministic
scripted simulation, not measured call audio. Every response still carries
the uniform control envelope; no tool publishes a capsule or promotes it
into a corpus.

## Output

On success: the identical envelope the CLI emits for `--format json`, schema
at [`https://hotato.dev/schema/envelope.v1.json`](https://hotato.dev/schema/envelope.v1.json)
(`schema_version` "1", additive-only -- new keys may appear, the documented
core never changes).

Every expected failure (missing / mono / mismatched / not-found file,
unknown suite, ambiguous input mode, or a well-formed input with no scorable
event) comes back as a structured error object, `ok: false`, with a stable
`error_code` and actionable `message` -- schema at
[`https://hotato.dev/schema/error.v1.json`](https://hotato.dev/schema/error.v1.json).
The MCP tool and CLI share this error shape: a caller, human or agent,
parses one contract across both surfaces.

## The fleet tools

Eleven tools drive the local, self-hosted fleet control plane
([`GUARDIAN-FLEET.md`](GUARDIAN-FLEET.md)): they read and reason over a
workspace, surfacing findings for a human to label and deploy.

| Tool | Scope | Does |
| --- | --- | --- |
| `fleet_status` | read-only | workspace counts + job-queue stats |
| `candidate_list` | read-only | top candidate moments awaiting a human label |
| `candidate_inspect` | read-only | one candidate's detail: onset, measured components (severity/input_health/recurrence/novelty/covered_by_contract), and trust findings |
| `contract_list` | read-only | contracts in a workspace |
| `trial_explain` | read-only | a recorded trial's verdict, evidence tier, and any pending human-gated action |
| `experiment_status` | read-only | a trial's current verdict, evidence tier, recommendation, and manifest hash |
| `artifact_verify` | read-only | verifies a contract bundle's authenticity + evidence on its own terms |
| `experiment_propose` | read-only | a bounded variant set with expected effects, ready for a human to clone, apply, or deploy |
| `experiment_create` | clone-scoped | precommit a trial manifest from a committed battery BEFORE any capture; capture and deploy stay separate, later steps |
| `experiment_run` | clone-scoped | recompute a before/after trial offline, entirely within the clone, and record a recommendation |
| `clone_cleanup` | clone-scoped | delete a STAGING clone an experiment created, scoped entirely to that clone |

## More

- Python API and the shared error contract: [`API.md`](API.md)
- What the envelope measures and the scope/ceiling: the top-level
  [`README.md`](../README.md) and [`METHODOLOGY.md`](../METHODOLOGY.md)
- Machine-readable index of every command (CLI and MCP): [`llms.txt`](../llms.txt)
