# The closed loop: find -> fix -> prove it's fixed

`hotato diagnose` / `inspect` / `plan` (see [FIX-PLANS.md](FIX-PLANS.md)) end at
a **proposal**. The closed loop carries it the rest of the way, with the two
irreversible decisions kept firmly in human hands:

1. **`hotato patch`** turns a plan's abstract `{field, from, to}` into a
   **literal, paste-ready artifact** for your platform. It PRODUCES the change;
   it never applies it.
2. You apply it, in your own stack, and re-capture the failing moments.
3. **`hotato verify`** scores the old and new runs against each other and gives
   you a **battery-scale before/after proof**: *N of M fixtures that used to
   fail now pass, and K of L hold fixtures still pass.*
4. **`hotato loop`** orchestrates the whole thing and **remembers where it left
   off**, so you can walk away and come back.

Nothing here mutates a platform, auto-labels a moment, or auto-applies a change.

## `hotato patch <fixplan.json>` -- the paste-ready change

Reads a fix plan (schema `hotato.fixplan.v1`) and renders it per platform:

- **Vapi / Retell** (config behind a REST API): a JSON **merge-patch body** plus
  a ready **`curl`** against the platform's real config-update endpoint, using
  the exact field names the plan carries.

  ```
  hotato plan result.json --stack vapi --assistant-id <id> --out fixplan.json
  hotato patch fixplan.json
  ```
  ```
  curl -X PATCH https://api.vapi.ai/assistant/<assistant-id> \
    -H "Authorization: Bearer $VAPI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"stopSpeakingPlan": {"numWords": 2}}'
  ```

- **LiveKit / Pipecat** (config lives in your agent source): there is no
  config-update REST call to hit, so patch emits the exact **source edit** --
  the constructor kwarg and the literal value to set -- never a fabricated
  endpoint. For example `InterruptionOptions(min_words=1)`.

- **generic / unknown stack**: patch names the knob **family** and asks for a
  concrete stack; it emits no literal body it cannot stand behind.

### The both-axes case: no patch, a pointer instead

`hotato patch` only handles the config-fixable classes. When the plan's decision
is `do_not_tune_single_threshold` -- the genuine **both-axes** case, where the
battery misses a real interruption AND false-stops on a backchannel at once -- no
single config value fixes both. patch emits **no config patch**. It prints the
vendor-neutral, numbers-free **engagement-control pointer** instead: it names the
problem class (discriminating a real bid for the floor from a backchannel) and
the KIND of fix it needs, names no product, and carries no digits. It is not an
upsell, and it fires ONLY on this case -- never on an ambiguous slow yield or a
coverage gap, which get their honest "no patch, here's why" instead.

Every other non-propose decision (diagnostic checklist, insufficient coverage,
already at a documented bound, no change) likewise emits no patch, with the
reason pointing back at the plan.

**Honesty:** patch makes no network call and pins `applies_change` to false.
`--format json` emits the full artifact; `--out PATH` also writes it.

## `hotato verify --before <old> --after <new>` -- the proof

After you apply the change and re-capture the same fixtures, verify scores the
old and new run envelopes against each other. Each side is a single `hotato run`
envelope JSON (a whole battery), or a directory of them; fixtures pair by
`event_id` (then `scenario_id`).

```
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio \
    --format json > before.json          # the failing take
# ... apply the patch, re-capture the fixtures ...
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio-new \
    --format json > after.json
hotato verify --before before.json --after after.json
```

It **reuses** the `hotato compare` taxonomy per fixture (`fixed`, `regressed`,
`improved`, `worse`, `unchanged`, `still_pass`, `not_scorable`) and aggregate's
pooled-distribution definitions for the before/after talk-over and time-to-yield
shift. The rollup is the two axes that matter:

- **regression axis**: how many previously-failing fixtures now pass;
- **hold axis**: how many hold-labeled guard fixtures still pass (they must not
  regress).

**Honesty:**

- verify reports **coincidence, never causation**: it says the improvement
  *coincides with* your change, never that it *caused* it. Hotato measures
  timing; it does not run a controlled experiment.
- it **refuses** the battery-scale claim when too few fixtures failed to
  characterize (`--min-n`, default 3): the per-fixture facts still print, but the
  headline proof is withheld and said so.
- an unjudgeable side is `not_scorable`, never an invented verdict; a fixture on
  only one side is reported unpaired, never silently folded into the rollup.

By default verify measures and exits 0; `--fail-on-regression` exits 1 if any
fixture regressed or got worse.

### `--policy hotato.verify.yaml` -- the anti-bandaid gate

`--policy` turns the measured rollup into a PASS/FAIL gate you can drop into CI.
A policy declares two things, and **both** must hold for verify to pass:

```yaml
target:
  improve:
    talk_over_sec_p95: -0.5   # pooled talk-over p95 must drop by >= 0.5s
    failed_count: decrease    # fewer fixtures may fail than before
guardrails:
  max_new_false_yields: 0     # no hold guard that passed before may newly yield
  max_not_scorable: 0         # every paired fixture must be judgeable
  require_hold_fixture: true  # the battery must contain a hold fixture...
  require_yield_fixture: true # ...and a yield fixture
```

```
hotato verify --before before.json --after after.json --policy hotato.verify.yaml
```

- **`target.improve`** is the success criteria. A signed number is a required
  delta (`after - before`), so `-0.5` means "must improve by at least 0.5"; a
  keyword (`decrease`, `increase`, `no_worse`, `no_better`, `unchanged`) states
  direction only. Metrics: `talk_over_sec_p95`, `seconds_to_yield_p95`,
  `failed_count`, `false_yield_count`.
- **`guardrails`** are hard fail conditions. `max_new_false_yields` and
  `max_not_scorable` cap what a naive threshold bandaid would silently trade in;
  `require_hold_fixture` / `require_yield_fixture` refuse to certify a battery
  that never even tests the opposite axis.

verify exits `1` unless **every guardrail holds AND every target is met**, so a
fix cannot pass by improving one axis while regressing (or never testing) the
other. A patch that cuts talk-over by making the agent yield to everything meets
the talk-over target but trips `max_new_false_yields` on the hold fixtures, and
the whole check fails. The guardrails and targets are shown in the `--out
verify.html` proof (a `Policy check: PASSED`/`FAILED` headline and an
ok/violated, met/unmet table) and in the text and JSON output.

The policy is parsed with the standard library only -- Hotato's core carries no
third-party runtime dependency -- over the small subset the shipped
`examples/verify-policy/hotato.verify.yaml` uses. An unknown key, a wrong-typed
value, an empty policy, a tab indent, or a list is a clean exit-2 usage error.

## `hotato loop [FOLDER]` -- one command, with memory

`hotato loop` drives the parts Hotato can drive and remembers where it left off
in a small local state file (`.hotato/loop-state.json` by default):

- **First run over a folder of calls**: it runs discovery (`analyze` -> `scan` ->
  rank) and records the candidate moments. Stage `awaiting_label`:

  > you have N candidate moment(s) awaiting your label.

- **You label the ones that matter** with `hotato fixture create` (loop never
  labels for you: only a human supplies the yield/hold intent).

- **Next run, with those fixtures present** (`--fixtures DIR`): it runs them,
  diagnoses the battery (including the threshold-funnel check), and plans a
  guarded fix. Stage `awaiting_verify`:

  > a fix plan is ready; apply it with hotato patch, then prove it with hotato
  > verify.

```
hotato loop ./recordings                          # run 1: discover
hotato fixture create --stereo rec.wav --onset 12.4 \
    --expect yield --id refund-001 --out tests/hotato
hotato loop ./recordings --fixtures tests/hotato   # run 2: plan
```

**Hard rules:** the loop NEVER auto-labels (you supply every yield/hold intent),
NEVER auto-applies (it produces a plan and points at `hotato patch`; applying and
verifying stay human steps), and mutates no platform. It orchestrates and tracks
state; you keep the two decisions that matter -- which moment is a real bug, and
whether to apply the fix.

## Exit codes

- `hotato patch`: `0` a patch (config artifact or the engagement-control
  pointer) was produced; `2` the input is not a fix plan or is unreadable.
- `hotato verify`: `0` the rollup was produced (a low-n claim is refused but
  still exits 0); `1` a gate you opted into failed -- with `--fail-on-regression`
  a fixture regressed or got worse, or with `--policy` a guardrail was violated
  or a `target.improve` criterion was not met; `2` usage error, unreadable
  input, an invalid `--policy` file, or no fixtures pair.
- `hotato loop`: `0` advanced or re-reported state; `2` no folder on the first
  run, an unreadable state file, or a path that is not a folder.
