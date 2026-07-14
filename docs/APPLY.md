# hotato apply: the guarded, clone-only staged apply

`hotato apply` is the last rung of the fix ladder and the ONLY command in Hotato
that can mutate external platform state. Because of that, it is the most
conservative command in the codebase: every flag combination leaves your
production/source assistant untouched. It reads a `hotato patch` artifact and
either PRINTS the fresh staging clone it would create (the default, fully
offline dry run) or, only with `--yes` and credentials, creates a NEW staging
assistant that is your source config with the patch applied.

```
hotato patch fixplan.json --format json --out patch.json
hotato apply patch.json --clone --name staging-refund-fix --battery tests/hotato
```

## The five hard rules, enforced in code

1. **Clone-only.** This version ships one apply path: staging-clone only. A
   non-`--clone` invocation is a clean usage error: `production apply is not
   supported; use --clone to apply to a fresh staging assistant`. The one
   writing call is a `POST` that creates a NEW assistant, never a `PUT`/`PATCH`
   against the source.

2. **Refusal-first.** If the patch is the both-axes threshold funnel (the plan
   decided `do_not_tune_single_threshold`), apply REFUSES before doing anything
   and prints the exact recommendation:

   ```
   No config patch will be applied
   Reason: both missed real interruption and false stop on backchannel, one threshold cannot safely fix both
   Recommended: enable or add engagement-control / backchannel-aware turn detection
   ```

   The refusal is a FEATURE. It exits with a distinct, documented code (`3`), so
   a script can tell "refused by design" apart from a usage error. A single
   sensitivity threshold trades against itself between catching an
   interruption and holding through a backchannel -- solving it takes a
   discrimination fix, and every clone's patch reflects that.

3. **Opposite-risk required.** apply requires `--battery` to carry BOTH a
   yield fixture (an interruption the agent must stop for) AND a hold
   fixture (a backchannel the agent must keep the floor through), so it can see
   the opposite risk a threshold move trades into before applying. Point
   `--battery` at your fixtures directory (with a `scenarios/` folder, as
   `hotato fixture promote` writes) or at a folder of run-envelope / scenario
   JSONs.

4. **Gated side effect.** The default is a dry run that prints exactly the clone
   it would create and the patch it would apply, fully offline. Only `--yes`
   WITH credentials reaches the platform. The create
   call is the only networked function: it reads the source config (`GET`),
   applies the patch to a copy, and creates a NEW assistant (`POST`).

5. **Name required.** The staging clone must be named explicitly (`--name`);
   apply always uses the name you give it.

## What gets cloned

Only the REST-config stacks have an assistant/agent to clone through an API:

- **vapi**
  - Read (source, read-only): `GET https://api.vapi.ai/assistant/{id}`
  - Create (a NEW assistant): `POST https://api.vapi.ai/assistant`
  - Name field: `name`
- **retell**
  - Read (source, read-only): `GET https://api.retellai.com/get-agent/{id}`
  - Create (a NEW assistant): `POST https://api.retellai.com/create-agent`
  - Name field: `agent_name`

The clone config is your source config with the patch deep-merged on top, given
the new name, and stripped of the server-assigned ids so it is a fresh object,
never an overwrite of the source.

LiveKit and Pipecat keep turn-taking config directly in your agent SOURCE, so
there is no assistant to clone through an API; `hotato patch` already emits the
exact source edit for those stacks, and apply points you straight at it.
Twilio carries the audio but runs no turn-taking agent, so apply points at the
upstream stack.

## After the clone: prove it

The clone is a staging assistant so you can prove the fix before touching
production. Re-capture the same battery through the SOURCE (into `before/`) and
through the CLONE (into `after/`), then verify:

```
hotato verify --before before/ --after after/ --policy hotato.verify.yaml
```

`hotato verify` reports coincidence, never causation, and the policy is the
anti-bandaid gate: the fix passes only if every guardrail holds (including
`max_new_false_yields` on the hold fixtures) AND every target is met, so a patch
that just makes the agent yield to everything is caught. See `docs/FIX-LOOP.md`.

Or run the whole thing (this gate + verify + an optional contracts re-verify +
explain's attribution) as one fail-closed before/after report:
`hotato fix trial patch.json --name staging-refund-fix --before before/ --after
after/`. See [`docs/FIX-TRIAL.md`](FIX-TRIAL.md).

## Exit codes

- `0` the staging clone was rendered (dry run by default; with `--yes` and
  credentials the NEW staging assistant was created and patched, the source
  untouched).
- `2` usage error: no `--clone`, no `--name`, no opposite-risk battery (both a
  yield and a hold fixture), a stack with no assistant to clone, a patch that
  produced no config change, or unreadable input.
- `3` principled refusal: the plan is the both-axes threshold funnel, so apply
  refuses a single-threshold patch by design. The refusal is the feature.
