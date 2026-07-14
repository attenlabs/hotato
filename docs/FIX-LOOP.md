# The closed loop: find -> fix -> prove it's fixed

`hotato diagnose` / `inspect` / `plan` (see [FIX-PLANS.md](FIX-PLANS.md))
end at a **proposal**. The closed loop carries it the rest of the way, with
the two irreversible decisions kept in human hands:

1. **`hotato patch`** turns a plan's abstract `{field, from, to}` into a
   **literal, paste-ready artifact** for your platform. It produces the
   artifact; applying it is your call.
2. You apply it, in your own stack, and re-capture the failing moments.
3. **`hotato verify`** scores the old and new runs against each other: a
   **battery-scale before/after proof** -- *N of M fixtures that used to
   fail now pass, K of L hold fixtures still pass.*
4. **`hotato loop`** orchestrates the whole thing and **remembers where it
   left off**, so you can walk away and come back.

Every step produces a proposal, a label, or a reviewable artifact -- you
decide what ships to your platform.

## `hotato patch <fixplan.json>`: the paste-ready change

Reads a fix plan (schema `hotato.fixplan.v1`) and renders it per platform:

- **Vapi / Retell** (config behind a REST API): a JSON **merge-patch body**
  plus a ready **`curl`** against the platform's config-update endpoint,
  using the exact field names the plan carries.

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

- **LiveKit / Pipecat** (config lives in your agent source): patch emits
  the exact **source edit** -- the constructor kwarg and literal value to
  set, e.g. `InterruptionOptions(min_words=1)`.

- **generic / unknown stack**: patch names the knob **family** and asks
  for a concrete stack; it emits no literal body it cannot stand behind.

### The both-axes case: no single knob fixes it

`hotato patch` handles config-fixable classes directly. When the plan's
decision is `do_not_tune_single_threshold` -- the **both-axes** case,
where the battery misses a real interruption AND false-stops on a
backchannel at once -- no single config value fixes both. patch prints a
vendor-neutral, numbers-free **engagement-control pointer** instead: the
problem class (telling a real bid for the floor apart from a backchannel)
and the KIND of fix it needs, no product named, no digits. It fires ONLY
here; an ambiguous slow yield or a coverage gap gets a "no patch, here's
why" explanation instead. Every other non-propose decision (diagnostic
checklist, insufficient coverage, already at a documented bound, no
change) likewise emits no patch, the reason pointing back at the plan.

**Guardrail:** patch runs entirely offline and pins `applies_change` to
false. `--format json` emits the full artifact; `--out PATH` also writes
it.

## `hotato verify --before <old> --after <new>`: the proof

After you apply the change and re-capture the same fixtures, verify scores
the old and new run envelopes against each other. Each side is a single
`hotato run` envelope JSON (a whole battery) or a directory of them;
fixtures pair by `event_id`, then `scenario_id`.

```
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio \
    --format json > before.json          # the failing take
# ... apply the patch, re-capture the fixtures ...
hotato run --scenarios tests/hotato/scenarios --audio tests/hotato/audio-new \
    --format json > after.json
hotato verify --before before.json --after after.json
```

It reuses the `hotato compare` taxonomy per fixture (`fixed`, `regressed`,
`improved`, `worse`, `unchanged`, `still_pass`, `not_scorable`) and
aggregate's pooled-distribution definitions for the before/after talk-over
and time-to-yield shift. Two axes matter:

- **regression axis** -- how many previously-failing fixtures now pass.
- **hold axis** -- how many hold-labeled guard fixtures still pass (they
  must not regress).

**Guardrail:**

- verify reports **coincidence**: the improvement *coincides with* your
  change. Hotato measures timing, not a controlled experiment, so
  coincidence is the strongest claim the evidence supports.
- it **refuses** the battery-scale claim when too few fixtures failed to
  characterize (`--min-n`, default 3): per-fixture facts still print, but
  the headline proof is withheld, and said so.
- an unjudgeable side reports `not_scorable`; a fixture on only one side is
  unpaired, kept out of the rollup.

By default verify measures and exits 0; `--fail-on-regression` exits 1 if
any fixture regressed or got worse.

### `--policy hotato.verify.yaml`: the anti-bandaid gate

`--policy` turns the measured rollup into a PASS/FAIL gate you can drop
into CI. A policy declares two things, and **both** must hold for verify
to pass:

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

- **`target.improve`** is the success criteria: a signed number is a
  required delta (`after - before` -- `-0.5` means "must improve by at
  least 0.5"); a keyword (`decrease`, `increase`, `no_worse`, `no_better`,
  `unchanged`) states direction only. Metrics: `talk_over_sec_p95`,
  `seconds_to_yield_p95`, `failed_count`, `false_yield_count`.
- **`guardrails`** are hard fail conditions: `max_new_false_yields` and
  `max_not_scorable` cap what a naive threshold bandaid would silently
  trade away; `require_hold_fixture` / `require_yield_fixture` make sure
  the battery tests the opposite axis before it can be certified.

verify exits `1` unless **every guardrail holds and every target is met**,
so a fix passes only when it improves one axis without regressing the
other. A patch that cuts talk-over by making the agent yield to everything
meets the talk-over target but trips `max_new_false_yields` on the hold
fixtures -- the whole check fails. The `--out verify.html` proof shows the
guardrails and targets (a `Policy check: PASSED`/`FAILED` headline, an
ok/violated, met/unmet table), same as text and JSON.

The policy is parsed with the standard library only -- Hotato's core
carries no third-party runtime dependency -- over the small subset the
shipped `examples/verify-policy/hotato.verify.yaml` uses. An unknown key,
a wrong-typed value, an empty policy, a tab indent, or a list is a clean
exit-2 usage error.

## `hotato loop [FOLDER]`: one command, with memory

`hotato loop` drives the parts Hotato can drive and remembers where it
left off, in a small local state file (`.hotato/loop-state.json` by
default):

- **First run over a folder of calls**: runs discovery (`analyze` ->
  `scan` -> rank) and records the candidate moments. Stage
  `awaiting_label`:

  > you have N candidate moment(s) awaiting your label.

- **You label the ones that matter** with `hotato fixture create` -- the
  yield/hold intent always comes from a human.

- **Next run, with those fixtures present** (`--fixtures DIR`): runs them,
  diagnoses the battery (including the threshold-funnel check), and plans
  a guarded fix. Stage `awaiting_verify`:

  > a fix plan is ready; apply it with hotato patch, then prove it with
  > hotato verify.

```
hotato loop ./recordings                          # run 1: discover
hotato fixture create --stereo rec.wav --onset 12.4 \
    --expect yield --id refund-001 --out tests/hotato
hotato loop ./recordings --fixtures tests/hotato   # run 2: plan
```

**What stays yours:** every yield/hold intent, and applying the fix (loop
produces a plan and points at `hotato patch`; applying and verifying stay
your steps). loop tracks state across runs, leaving your platform
untouched -- you keep the two decisions that matter: which moment is
real, and whether to fix it.

## Exit codes

| Command | Exit | Meaning |
|---|---|---|
| `hotato patch` | `0` | a patch (config artifact or the engagement-control pointer) was produced |
| `hotato patch` | `2` | input is not a fix plan, or is unreadable |
| `hotato verify` | `0` | the rollup was produced (a low-n claim is refused but still exits 0) |
| `hotato verify` | `1` | a gate you opted into failed: `--fail-on-regression` and a fixture regressed or got worse, or `--policy` and a guardrail was violated or a `target.improve` criterion wasn't met |
| `hotato verify` | `2` | usage error, unreadable input, an invalid `--policy` file, or no fixtures pair |
| `hotato loop` | `0` | advanced or re-reported state |
| `hotato loop` | `2` | no folder on the first run, an unreadable state file, or a path that isn't a folder |
