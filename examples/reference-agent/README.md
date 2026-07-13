# Reference agent — the complete Conversation-QA Foundation 1.3 example

A self-contained, runnable example of the whole hotato conversation-QA loop,
offline. It is a **reference voice agent under test**: 25 realistic jobs, most
handled correctly and a handful with genuine defects, so the suite surfaces
failures the way a working QA system does.

```
25 scenarios  ×  5 caller behaviours (speaking paces 0.7–1.4×)  ×  3 audio
environments (clean / cafe / street)  =  375 offline simulated runs
```

Everything runs through the **deterministic scripted-caller simulator** — no
live agent, no network, no model. Each scenario declares a scripted caller plus
a deterministic `agent_mock` (tool calls + a post-call state sandbox), so the
conversation-tests exercise the **outcome** and **policy** authorities offline:

- **Authority 1 — the trace:** `agent_mock.tools` render as `tool_call` spans;
  `tool_result` / `tool_error` / `sequence` read them, never the agent's words.
- **Authority 2 — the state sandbox:** `agent_mock.state` is a `{resource: rows}`
  post-call system of record; `state` / `state_change` query it.

A mock is never a live agent: every produced conversation is `origin=simulated`,
and its evidence is labelled the simulator's.

## Run it

```bash
make reference          # regenerate the files, run the 375-run suite, print counts + wall time
make serve              # browse the recorded workspace (hotato serve)
make clean              # remove ./.workspace and ./.out
```

Or directly:

```bash
python generate.py                       # write scenarios/, tests/, suite.json
python run_reference.py --parallel 8     # run the suite; record a browsable workspace
hotato serve --workspace reference --registry ./.workspace
```

`run_reference.py` prints the real counts (runs, valid, per-dimension
pass/fail/inconclusive, `SIMULATOR_INVALID`) and the wall time, records the
Release / Suite / Scenario / Run / Conversation / Evaluation rows into a local
fleet registry under `./.workspace`, and writes the per-test conversation
artifacts + the suite report under `./.out`.

## Layout

```
scenarios/<job>.scenario.json   # 25 hotato.scenario.v1 files (caller + agent_mock)
tests/<job>.test.json           # 25 hotato.conversation-test.v1 files (deterministic assertions)
suite.json                      # the hotato.suite.v1 binding them (required_for_release, inconclusive_policy: fail)
generate.py                     # the data-driven builder (source of truth for the files)
run_reference.py                # runs the suite + records the workspace
Makefile                        # make reference / serve / clean
```

## What the tests check, per dimension

| Dimension | Assertion kinds used |
| --- | --- |
| Outcome | `tool_result` (Authority 1), `state` (Authority 2) |
| Policy | `sequence` (identity verified before the sensitive action), `tool_call` (required disclosure), `handoff` (escalation), `termination`, `tool_error` (absent) |
| Conversation | `phrase` (the caller stated their need), `count`, `sequence` (ordered flow) |
| Speech | `latency` (a tool responded within its budget) |
| Reliability | pass@1 / pass@k / pass^k over the runs (its own axis, never blended) |

Success is a boolean over named conditions; the scorecard is per-dimension.
There is no blended or overall score anywhere.

## The built-in defects

Four jobs carry genuine agent bugs, so the suite reports real failures — the raw
material for the failure-cluster view and the production-to-regression flow:

- `refund-claimed-not-issued` — the agent says the refund is done but never calls
  `issue_refund` → **outcome** FAIL (`tool_result` + `state`).
- `identity-skipped-before-lookup` — records looked up before identity is verified
  → **policy** FAIL (`sequence`).
- `escalate-not-handed-off` — a manager was requested but no handoff occurred →
  **policy** FAIL (`handoff`).
- `payment-declined-handled-wrong` — the charge errored but the flow required no
  error → **policy** FAIL (`tool_error`).

The suite therefore exits non-zero, and `hotato serve` clusters these failures by
observable signature.

See [../../docs/SUITE-RUN.md](../../docs/SUITE-RUN.md),
[../../docs/CONVERSATION-TEST.md](../../docs/CONVERSATION-TEST.md), and
[../../docs/STATE-ADAPTERS.md](../../docs/STATE-ADAPTERS.md).
