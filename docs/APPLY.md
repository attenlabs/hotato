# hotato apply: the guarded, clone-only staged apply

`hotato apply` turns a `hotato patch` artifact into a staging assistant
you test before touching production. It's the only Hotato command that
mutates external platform state, and the most conservative one for it:
every flag combination leaves your source assistant untouched. By
default it PRINTS the clone it would create, an offline dry run; only
`--yes` with credentials creates the new one.

```
hotato patch fixplan.json --format json --out patch.json
hotato apply patch.json --clone --name staging-refund-fix --battery tests/hotato
```

## Five rules, enforced in code

1. **Clone-only.** apply ships one path: staging-clone. Any call without
   `--clone` errors cleanly: `production apply is not supported; use
   --clone to apply to a fresh staging assistant`. The only write is a
   `POST` creating a NEW assistant, never `PUT`/`PATCH` on the source.

2. **Refusal-first.** If the patch is the both-axes threshold funnel (the
   plan decided `do_not_tune_single_threshold`), apply REFUSES before
   doing anything and prints the exact recommendation:

   ```
   No config patch will be applied
   Reason: both missed real interruption and false stop on backchannel, one threshold cannot safely fix both
   Recommended: enable or add engagement-control / backchannel-aware turn detection
   ```

   The refusal is a FEATURE: a distinct, documented exit code (`3`) so a
   script can tell "refused by design" from a usage error. A single
   sensitivity threshold trades catching an interruption against holding
   through a backchannel; fixing it takes a discrimination fix, and every
   clone's patch reflects that.

3. **Opposite-risk required.** `--battery` must carry BOTH a yield
   fixture (an interruption the agent must stop for) AND a hold fixture
   (a backchannel it must keep the floor through), so apply can see the
   opposite risk before applying. Point `--battery` at your fixtures
   directory (with a `scenarios/` folder, as `hotato fixture promote`
   writes) or a folder of run-envelope / scenario JSONs.

4. **Gated side effect.** The default is a dry run: it prints the clone
   and patch, offline. Only `--yes` with credentials reaches the
   platform. The create call is the only networked step: read the source
   config (`GET`), apply the patch to a copy, create a NEW assistant
   (`POST`).

5. **Name required.** The staging clone must be named explicitly
   (`--name`); apply always uses the name you give it.

## What gets cloned

The REST-config stacks clone an assistant/agent through their own API,
read-only against the source and creating a NEW one:

| Stack | Read (source) | Create (new) | Name field |
| --- | --- | --- | --- |
| vapi | `GET https://api.vapi.ai/assistant/{id}` | `POST https://api.vapi.ai/assistant` | `name` |
| retell | `GET https://api.retellai.com/get-agent/{id}` | `POST https://api.retellai.com/create-agent` | `agent_name` |

The clone config is your source config with the patch deep-merged on top,
the new name, and server ids stripped: a fresh object, never an
overwrite of the source.

LiveKit and Pipecat keep turn-taking config in your agent source, so
`hotato patch` emits the exact source edit and apply points straight at
it. Twilio carries the audio but runs no turn-taking agent, so apply
points at the upstream stack instead.

## After the clone: prove it

The clone lets you prove the fix before touching production. Re-capture
the same battery through the SOURCE (`before/`) and the CLONE (`after/`),
then verify:

```
hotato verify --before before/ --after after/ --policy hotato.verify.yaml
```

`hotato verify` reports coincidence, not causation. Its policy is the
anti-bandaid gate: a fix passes only when every guardrail holds
(including `max_new_false_yields` on the hold fixtures) and every target
is met -- a patch that just makes the agent yield to everything gets
caught. See `docs/FIX-LOOP.md`.

Or run the whole thing (this gate, verify, an optional contracts
re-verify, and explain's attribution) as one fail-closed before/after
report: `hotato fix trial patch.json --name staging-refund-fix --before
before/ --after after/`. See [`docs/FIX-TRIAL.md`](FIX-TRIAL.md).

## Exit codes

- `0` the staging clone was rendered (dry run by default; with `--yes`
  and credentials the NEW staging assistant was created and patched, the
  source untouched).
- `2` usage error: no `--clone`, no `--name`, no opposite-risk battery (a
  yield and a hold fixture), a stack with no assistant to clone, a patch
  with no config change, or unreadable input.
- `3` principled refusal: the plan is the both-axes threshold funnel, so
  apply refuses a single-threshold patch by design -- the refusal is the
  feature.
